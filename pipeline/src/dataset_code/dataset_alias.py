"""Derive dataset-name aliases and lexically prefilter paper x dataset pairs.

Step 2.1 asks one LLM call per paper to map the paper's extracted datasets onto
catalog entries. This module picks the candidate entries for that call: the
catalog datasets with a lexical trace in the paper text.

No alias field exists in the dataset schema, so aliases are *synthesized* per
dataset from three signals:
  1. the pool string itself (usually a slug or a humanized name),
  2. any rich catalog record present (slug / dataset_name / description),
  3. the free-text ``dataset_name`` values the extractor wrote into each paper's
     ``extraction['dataset_details.md']['datasets']`` (populated for ~1.5k
     papers), which routinely carry the "Full Name (ACRONYM)" pattern.

Robustness to name variation is the whole point, so each dataset gets three
match tiers:
  * **acronyms**   -- short all-caps/alnum tokens (TCGA, TCGA-BRCA, PANDA),
    matched case-sensitively as whole tokens so "the"/"and" never hit.
  * **full_forms** -- multiword long names, matched as normalized substrings
    ("the cancer genome atlas" inside the normalized paper text).
  * **core_tokens**-- the significant word tokens of the name minus generic
    words, with a year normalizer so ``camelyon16`` == ``camelyon-2016``.

The gate only ever *removes* candidates; every surviving one is still adjudicated
by the LLM, so it cannot create false positives in the output. The only quality
risk is recall (dropping a true match).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from pipeline.src.paper_code.dedupe import normalize_slug

# Words that carry no discriminating signal in a dataset name. A name reduced to
# only these has no usable core tokens and falls back to acronym/full-form tiers.
_GENERIC_WORDS = {
    "dataset", "datasets", "data", "database", "db", "challenge", "grand",
    "the", "a", "an", "of", "for", "and", "set", "cohort", "collection",
    "benchmark", "study", "project", "corpus", "repository", "public",
    "images", "image", "slides", "slide", "whole", "digital", "pathology",
    "histopathology", "histopathological",
}

# "Full Name (ACRONYM)" -- e.g. "The Cancer Genome Atlas Breast ... (TCGA-BRCA)".
_ACRONYM_PAREN_RE = re.compile(r"([A-Za-z][\w &/,'-]{3,}?)\s*\(([A-Z0-9][A-Z0-9-]{1,15})\)")
# A standalone acronym-shaped token inside a name: TCGA, PANDA, NCT-CRC-HE-100K.
_ACRONYM_TOKEN_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,}(?:-[A-Z0-9]+)*)\b")
# An author citation inside a name: "Colorectal adenocarcinoma histology dataset (Kather
# et al., 2016)". Papers often name such a dataset only that way ("the Kather dataset
# [21] ... introduced by Kather et al."), with none of its other names.
_AUTHOR_CITE_RE = re.compile(r"\b([A-Z][A-Za-z'-]{2,})\s+et\s+al\.?,?\s*\(?(?:19|20)\d\d")

_ALNUM_SPLIT_RE = re.compile(r"([a-z]+)|(\d+)")


def _norm_text(s: str) -> str:
    """Lowercase, punctuation -> single spaces, collapsed. For substring tests."""
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _year_variants(num: str) -> Set[str]:
    """Map a numeric token to its year-equivalent forms.

    ``16`` -> {16, 2016}; ``2016`` -> {2016, 16}. This is what makes
    ``camelyon16`` and ``camelyon-2016`` compare equal. Two-digit values are
    treated as 20xx (dataset years in this domain are all 2000s).
    """
    out = {num}
    if len(num) == 2 and num.isdigit():
        out.add("20" + num)
    elif len(num) == 4 and num.startswith("20"):
        out.add(num[2:])
    return out


def _explode_token(tok: str) -> Set[str]:
    """Expand one token into its comparable forms.

    ``camelyon16`` -> {camelyon16, camelyon, 16, 2016}. Splitting the mixed
    letter/digit run lets a slug like ``camelyon16`` match a spaced name like
    ``camelyon 2016`` and vice-versa.
    """
    forms: Set[str] = {tok}
    if tok.isdigit():
        return forms | _year_variants(tok)
    parts = [g or h for g, h in _ALNUM_SPLIT_RE.findall(tok)]
    if len(parts) > 1:
        for p in parts:
            forms.add(p)
            if p.isdigit():
                forms |= _year_variants(p)
    return forms


def tokenize(text: str) -> Set[str]:
    """Token set for containment tests, with mixed letter/digit runs exploded."""
    tokens: Set[str] = set()
    for raw in _norm_text(text).split():
        tokens |= _explode_token(raw)
    return tokens


@dataclass
class AliasSet:
    """Match signals for one dataset (one pool string)."""

    slug: str
    acronyms: Set[str] = field(default_factory=set)      # case-sensitive whole-token
    full_forms: Set[str] = field(default_factory=set)    # normalized substrings
    core_tokens: Set[str] = field(default_factory=set)   # all must be present

    def surfaces(self) -> List[str]:
        """Candidate surface strings to center an excerpt on, longest first."""
        out = set(self.acronyms) | set(self.full_forms)
        return sorted(out, key=len, reverse=True)


def _significant_tokens(name: str) -> Set[str]:
    """Core word tokens of a name: the pure-letter parts, minus generic words.

    A mixed letter/digit run (``camelyon16``) contributes only its letter part
    (``camelyon``) -- never the joined form, which a paper that writes
    "CAMELYON 2016" would not contain -- and never the bare year, which is not
    discriminating on its own.
    """
    core: Set[str] = set()
    for raw in _norm_text(name).split():
        if raw in _GENERIC_WORDS or raw.isdigit():
            continue
        parts = [g or h for g, h in _ALNUM_SPLIT_RE.findall(raw)]
        candidates = parts if len(parts) > 1 else [raw]
        for p in candidates:
            if p.isalpha() and len(p) >= 3 and p not in _GENERIC_WORDS:
                core.add(p)
    return core


def _harvest_acronyms(text: str) -> Set[str]:
    acr: Set[str] = set()
    if not text:
        return acr
    for _, a in _ACRONYM_PAREN_RE.findall(text):
        acr.add(a)
    for a in _ACRONYM_TOKEN_RE.findall(text):
        # A single all-caps word that is a real English word (e.g. "WSI", "AND")
        # is still fine: case-sensitive whole-token matching means it only fires
        # when the paper also writes it in caps. Keep length >= 2.
        if len(a) >= 2:
            acr.add(a)
    return acr


def _add_name_to_alias(alias: AliasSet, name: str) -> None:
    """Fold one name string (long form or acronym) into an AliasSet."""
    name = (name or "").strip()
    if not name:
        return
    alias.acronyms |= _harvest_acronyms(name)
    for author in _AUTHOR_CITE_RE.findall(name):
        alias.full_forms.add(f"{author.lower()} et al")
    norm = _norm_text(name)
    # A multiword normalized name is usable as a full-form substring.
    if norm and " " in norm and len(norm) >= 6:
        alias.full_forms.add(norm)
    alias.core_tokens |= _significant_tokens(name)


def build_alias_catalog(
    pool: Iterable[str],
    rich_by_slug: Optional[Dict[str, dict]] = None,
    extraction_names_by_slug: Optional[Dict[str, Set[str]]] = None,
) -> Dict[str, AliasSet]:
    """Build ``pool_string -> AliasSet`` for every dataset in the match pool.

    Args:
        pool: the list of dataset strings step 2.1 iterates (slugs / names).
        rich_by_slug: optional ``normalized_slug -> record`` map from a prior
            ``datasets_seed_step2.json`` / ``initial_dataset_list.csv`` to pull
            dataset_name + description for extra aliases.
        extraction_names_by_slug: optional ``normalized_slug -> {free-text
            names}`` harvested from papers' dataset_details extractions.
    """
    rich_by_slug = rich_by_slug or {}
    extraction_names_by_slug = extraction_names_by_slug or {}
    catalog: Dict[str, AliasSet] = {}

    for pool_string in pool:
        if pool_string in catalog:
            continue
        slug = normalize_slug(pool_string)
        alias = AliasSet(slug=slug)

        # (1) the pool string itself -- often a slug like "camelyon16" or a
        # humanized "Camelyon 16". Treat as a name; _significant_tokens already
        # reduces "camelyon16" to its letter core.
        _add_name_to_alias(alias, pool_string)
        _add_name_to_alias(alias, slug.replace("-", " "))

        # (2) rich catalog record for this slug.
        rec = rich_by_slug.get(slug)
        if rec:
            _add_name_to_alias(alias, rec.get("dataset_name", ""))
            # descriptions carry the "(ACRONYM)" pattern and spelled-out forms.
            desc = rec.get("description", "") or ""
            alias.acronyms |= _harvest_acronyms(desc)
            for long_form, _acr in _ACRONYM_PAREN_RE.findall(desc):
                _add_name_to_alias(alias, long_form.strip())

        # (3) free-text names the extractor wrote for this slug across papers.
        for name in extraction_names_by_slug.get(slug, set()):
            _add_name_to_alias(alias, name)

        catalog[pool_string] = alias

    return catalog


_CASED_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")


@dataclass
class PaperText:
    """Precomputed, reusable match surfaces for a single paper."""

    tokens: Set[str]
    norm_text: str              # lowercased/normalized, for full-form substrings
    cased_tokens: Set[str]      # original-case whole tokens, for acronym matching
    extraction_slugs: Set[str]  # slugs the paper's own extraction named


def build_paper_text(paper: dict) -> PaperText:
    """Gather and precompute a paper's searchable text once."""
    parts: List[str] = []
    for key in ("title", "snippet", "fulltext"):
        v = paper.get(key)
        if isinstance(v, str) and v:
            parts.append(v)

    extraction = paper.get("extraction") or {}
    sp = extraction.get("sum_paper.md")
    if isinstance(sp, dict):
        ab = sp.get("Abstract")
        if isinstance(ab, str):
            parts.append(ab)

    extraction_slugs: Set[str] = set()
    dd = extraction.get("dataset_details.md")
    if isinstance(dd, dict):
        for ds in dd.get("datasets", []) or []:
            if not isinstance(ds, dict):
                continue
            for nk in ("dataset_name", "name"):
                nm = ds.get(nk)
                if isinstance(nm, str) and nm.strip():
                    parts.append(nm)
                    extraction_slugs.add(normalize_slug(nm))
            sl = ds.get("slug")
            if isinstance(sl, str) and sl.strip():
                extraction_slugs.add(normalize_slug(sl))

    raw_text = "\n".join(parts)
    cased: Set[str] = set()
    for tok in _CASED_TOKEN_RE.findall(raw_text):
        cased.add(tok)
        if "-" in tok:  # keep sub-parts so acronym "TCGA" hits inside "TCGA-BRCA"
            cased.update(tok.split("-"))
    return PaperText(
        tokens=tokenize(raw_text),
        norm_text=_norm_text(raw_text),
        cased_tokens=cased,
        extraction_slugs=extraction_slugs,
    )


def prefilter_match(paper_text: PaperText, alias: AliasSet) -> Tuple[bool, Optional[str], str]:
    """Decide whether a paper x dataset pair should reach the LLM.

    Returns ``(keep, surface, reason)`` where ``surface`` is the matched alias
    string to center the LLM excerpt on (None if kept only by the guard), and
    ``reason`` is one of ``extraction_guard`` / ``acronym`` / ``full_form`` /
    ``core_tokens`` / ``no_match`` (for diagnostics in the recall replay).
    """
    # High-precision guard: the paper's own heavy-model extraction named this
    # dataset. Always keep, regardless of surface form in the body text.
    if alias.slug and alias.slug in paper_text.extraction_slugs:
        return True, None, "extraction_guard"

    for acr in alias.acronyms:
        if acr in paper_text.cased_tokens:
            return True, acr, "acronym"

    for form in alias.full_forms:
        if form and form in paper_text.norm_text:
            return True, form, "full_form"

    if alias.core_tokens and alias.core_tokens <= paper_text.tokens:
        return True, None, "core_tokens"

    return False, None, "no_match"
