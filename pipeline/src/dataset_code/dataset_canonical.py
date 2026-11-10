"""Group dataset-name variants under one canonical dataset (step 1.5b).

The previous 1.5b handed the model alphabetized batches of slugs and asked for
the survivors back. Variants that sort far apart (`tcga-luad` vs
`the-cancer-genome-atlas-lung-adenocarcinoma`, `kather-100k` vs
`100-000-histological-images-...`) never shared a batch, the cross-batch pass
was a single ~1,000-slug call, and only the survivors came back -- so nothing
downstream could map a variant to its canonical dataset. On the January corpus
it removed 7% (1,138 -> 1,057) and left the validated catalog with 122 TCGA
entries.

This version splits the work the way the problem splits:

  1. Blocking (no LLM judgment): union-find over three cheap signals -- name
     embeddings, rare shared name tokens, and shared dataset URLs -- so every
     plausible duplicate lands in the same small block.
  2. Grouping (LLM): one call per multi-entry block, with the paper-written
     descriptions, returning explicit groups. Every input must be assigned
     exactly once; anything the model drops becomes its own dataset.

It returns the member -> canonical map, plus each canonical dataset's names
and descriptions so later steps (pre-validation, the 2.1 alias catalog) can use
the evidence the papers already wrote.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set
from urllib.parse import urlparse

import numpy as np

from pipeline.utils.llms.llm import run_gpt, validate_json_response

logger = logging.getLogger(__name__)

PROMPT = Path("pipeline/config/prompts/dataset_prompts/canonicalize_datasets.md")
PARENTS_PATH = Path("pipeline/config/supp/dataset_parents.json")

# Cohort families: a paper that uses TCGA-KICH also uses TCGA-RCC data, and every
# TCGA cohort sits under the umbrella TCGA entry. Datasets DERIVED from TCGA (patch
# sets, TIL maps) are not subsets and get no parent from this rule.
TCGA_FAMILIES = {
    "TCGA-RCC": {"TCGA-KIRC", "TCGA-KIRP", "TCGA-KICH"},
    "TCGA-NSCLC": {"TCGA-LUAD", "TCGA-LUSC"},
    "TCGA-COADREAD": {"TCGA-COAD", "TCGA-READ"},
    "TCGA-GBM/LGG": {"TCGA-GBM", "TCGA-LGG"},
}
_TCGA_NAME = re.compile(r"^TCGA[-\s]?([A-Z]{2,8}(?:/[A-Z]{2,5})?)$")
# "The Cancer Genome Atlas Bladder Urothelial Carcinoma (TCGA-BLCA)": a cohort under its
# long name. Derived sets ("TCGA-TIL patches") match neither pattern.
_TCGA_LONG_NAME = re.compile(r"^THE CANCER GENOME ATLAS\b.*\(TCGA-([A-Z]{2,8})\)$")

# Words that say nothing about WHICH dataset a name refers to.
_GENERIC = {
    "dataset", "datasets", "data", "database", "set", "collection", "challenge",
    "grand", "contest", "competition", "images", "image", "the", "of", "and",
    "for", "in", "on", "a", "an", "with", "from", "by", "public", "open",
    "histopathology", "histopathological", "histology", "histological",
    "pathology", "whole", "slide", "slides", "wsi", "wsis", "digital", "tissue",
}
_URL_HOSTS_TOO_GENERIC = {
    "github.com", "zenodo.org", "kaggle.com", "www.kaggle.com", "drive.google.com",
    "figshare.com", "doi.org", "dx.doi.org", "arxiv.org", "grand-challenge.org",
    "portal.gdc.cancer.gov", "www.cancerimagingarchive.net", "cancerimagingarchive.net",
}


def slugify(name: str) -> str:
    """Same slug rule as build_dataset_lists, so keys join with the seed list."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower())
    return re.sub(r"-+", "-", s).strip("-")


_WINDOWS_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_dir_name(slug: str, windows: bool = os.name == "nt") -> str:
    """`slug` as one folder name. Unchanged on macOS and Linux, where the dataset folders
    already on disk are named by it. On Windows, characters it forbids (the ':' in
    "Histo-Seg: H&E ...", the '/' in "TCGA-GBM/LGG") become '-', trailing dots and spaces
    go, a device name (CON, NUL.tar) gets a '-', and a name over 255 bytes is cut and
    given a hash."""
    if not windows:
        return slug
    s = _WINDOWS_FORBIDDEN.sub("-", slug).rstrip(". ")
    stem = s.split(".")[0]
    if stem.lower() in _WINDOWS_RESERVED:
        s = stem + "-" + s[len(stem):]
    if len(s.encode("utf-8")) > 255:
        cut = s.encode("utf-8")[:240].decode("utf-8", "ignore").rstrip(". -")
        s = f"{cut}-{hashlib.sha1(s.encode('utf-8')).hexdigest()[:8]}"
    return s or "-"


@dataclass
class Evidence:
    names: Set[str] = field(default_factory=set)
    descriptions: List[str] = field(default_factory=list)
    urls: Set[str] = field(default_factory=set)


@dataclass
class CanonicalDataset:
    slug: str
    name: str
    members: List[str]
    descriptions: List[str]
    names: List[str]
    note: str = ""


def collect_evidence(papers: Iterable[dict]) -> Dict[str, Evidence]:
    """slug -> names, descriptions and URLs the paper extractions wrote for it."""
    ev: Dict[str, Evidence] = defaultdict(Evidence)
    for p in papers:
        dd = (p.get("extraction") or {}).get("dataset_details.md") or {}
        for ds in dd.get("datasets") or []:
            if not isinstance(ds, dict):
                continue
            name = (ds.get("dataset_name") or ds.get("slug") or "").strip()
            slug = slugify(name)
            if not slug:
                continue
            e = ev[slug]
            e.names.add(name)
            desc = " ".join(str(ds.get("description") or "").split())
            if desc and desc not in e.descriptions:
                facts = ", ".join(f"{k}={ds[k]}" for k in
                                  ("domain", "modality", "staining_type", "tissue_site", "species")
                                  if ds.get(k))
                e.descriptions.append(desc + (f" ({facts})" if facts else ""))
            for k in ("home_page_url", "download_url", "code_url"):
                u = _norm_url(ds.get(k))
                if u:
                    e.urls.add(u)
    return dict(ev)


def _norm_url(u) -> str:
    if not isinstance(u, str) or not u.strip():
        return ""
    p = urlparse(u.strip() if "://" in u else "https://" + u.strip())
    host = (p.netloc or "").lower().removeprefix("www.")
    path = p.path.rstrip("/").lower()
    # A bare generic host (github.com, zenodo.org, ...) identifies nothing.
    if not host or (host in _URL_HOSTS_TOO_GENERIC and path.count("/") < 2):
        return ""
    return host + path


def _tokens(slug: str) -> Set[str]:
    out = set()
    for t in slug.split("-"):
        t = re.sub(r"(19|20)(\d{2})$", r"\2", t)   # camelyon2016 ~ camelyon16
        if len(t) >= 3 and t not in _GENERIC:
            out.add(t)
        # `camelyon16` must meet `camelyon-2016`: also index the letter stem of a
        # letters+digits token. Releases that share a stem (16 vs 17) only share
        # a block; the grouping call keeps them apart.
        stem = re.match(r"([a-z]{4,})\d+$", t)
        if stem and stem.group(1) not in _GENERIC:
            out.add(stem.group(1))
    return out


class _UnionFind:
    def __init__(self, items):
        self.p = {i: i for i in items}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _embed(texts: List[str], use_gemini: bool) -> Optional[List[List[float]]]:
    """Name embeddings; None (blocking falls back to tokens + URLs) on failure."""
    try:
        if use_gemini:
            from pipeline.utils.llms.llm import _get_gemini_client as client_fn
            model = os.getenv("WSI_EMBED_MODEL", "gemini-embedding-2")
        else:
            from pipeline.utils.llms.llm import _get_openai_client as client_fn
            model = os.getenv("WSI_EMBED_MODEL", "text-embedding-3-small")
        client = client_fn()
        out: List[List[float]] = []
        for i in range(0, len(texts), 512):
            resp = client.embeddings.create(model=model, input=texts[i:i + 512])
            out += [d.embedding for d in resp.data]
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Embedding failed ({e}); blocking on tokens and URLs only")
        return None


def build_blocks(slugs: List[str], evidence: Dict[str, Evidence], use_gemini: bool = False,
                 sim_threshold: float = 0.80, neighbors: int = 6,
                 max_token_df: int = 25) -> List[List[str]]:
    """Group slugs that might be the same dataset. Over-merging here is cheap
    (the LLM splits blocks); under-merging is what lets duplicates survive."""
    uf = _UnionFind(slugs)

    # (1) rare shared tokens -- `luad`, `camelyon`, `glas` -- but not `cancer`.
    toks = {s: _tokens(s) for s in slugs}
    df = Counter(t for ts in toks.values() for t in ts)
    by_tok: Dict[str, List[str]] = defaultdict(list)
    for s, ts in toks.items():
        for t in ts:
            if df[t] <= max_token_df:
                by_tok[t].append(s)
    for members in by_tok.values():
        for m in members[1:]:
            uf.union(members[0], m)

    # (2) shared specific dataset URL.
    by_url: Dict[str, List[str]] = defaultdict(list)
    for s in slugs:
        for u in evidence.get(s, Evidence()).urls:
            by_url[u].append(s)
    for members in by_url.values():
        for m in members[1:]:
            uf.union(members[0], m)

    # (3) embeddings of name + first description: catches acronym vs spelled-out.
    texts = []
    for s in slugs:
        e = evidence.get(s)
        name = sorted(e.names, key=len)[-1] if e and e.names else s.replace("-", " ")
        desc = e.descriptions[0][:200] if e and e.descriptions else ""
        texts.append(f"{name}. {desc}".strip())
    vecs = _embed(texts, use_gemini)
    if vecs:
        m = np.asarray(vecs, dtype=np.float32)
        m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-9
        sims = m @ m.T
        np.fill_diagonal(sims, -1.0)
        k = min(neighbors, len(slugs) - 1)
        if k > 0:
            top = np.argpartition(-sims, k - 1, axis=1)[:, :k]
            for i, row in enumerate(top):
                for j in row:
                    if sims[i, j] >= sim_threshold:
                        uf.union(slugs[i], slugs[int(j)])

    groups: Dict[str, List[str]] = defaultdict(list)
    for s in slugs:
        groups[uf.find(s)].append(s)
    return sorted(groups.values(), key=len, reverse=True)


def _group_block(block: List[str], evidence: Dict[str, Evidence], prompt: str,
                 model: str, use_gemini: bool) -> List[dict]:
    """One LLM call -> [{canonical_name, members:[slug], note}] covering every slug."""
    lines = []
    for i, s in enumerate(block):
        e = evidence.get(s)
        names = "; ".join(sorted(e.names)[:3]) if e and e.names else s.replace("-", " ")
        desc = (e.descriptions[0][:240] if e and e.descriptions else "")
        lines.append(f"{i}: {names}" + (f" -- {desc}" if desc else ""))
    user = "Dataset entries:\n" + "\n".join(lines)
    parsed = None
    try:
        resp = run_gpt(system_prompt=prompt, user_prompt=user, model=model, item="|".join(block)[:500],
                       response_format={"type": "json_object"}, use_gemini=use_gemini,
                       validate_json=True, reject_all_empty=False, max_retries=2)
        ok, parsed, _ = validate_json_response(resp.choices[0].message.content,
                                               reject_all_empty=False)
        parsed = parsed if ok and isinstance(parsed, dict) else None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Canonicalization call failed for a {len(block)}-entry block: {e}")

    out, seen = [], set()
    for g in (parsed or {}).get("groups") or []:
        if not isinstance(g, dict):
            continue
        members = []
        for m in g.get("members") or []:
            try:
                idx = int(m)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(block) and idx not in seen:
                seen.add(idx)
                members.append(block[idx])
        if members:
            out.append({"canonical_name": str(g.get("canonical_name") or "").strip()
                        or members[0].replace("-", " "),
                        "members": members, "note": str(g.get("note") or "").strip()})
    # Anything the model skipped stays its own dataset rather than vanishing.
    for idx, s in enumerate(block):
        if idx not in seen:
            out.append({"canonical_name": s.replace("-", " "), "members": [s], "note": ""})
    return out


def canonicalize_datasets(slugs: List[str], papers: Iterable[dict] = (),
                          use_gemini: bool = False,
                          extra_evidence: Optional[Dict[str, Evidence]] = None,
                          workers: int = 32) -> Dict[str, CanonicalDataset]:
    """Return canonical_slug -> CanonicalDataset covering every input slug once."""
    slugs = list(dict.fromkeys(s for s in (slugify(x) for x in slugs) if s))
    evidence = collect_evidence(papers)
    for s, e in (extra_evidence or {}).items():
        tgt = evidence.setdefault(s, Evidence())
        tgt.names |= e.names
        tgt.urls |= e.urls
        tgt.descriptions += [d for d in e.descriptions if d not in tgt.descriptions]

    blocks = build_blocks(slugs, evidence, use_gemini=use_gemini)
    multi = [b for b in blocks if len(b) > 1]
    logger.info(f"Canonicalization: {len(slugs)} names -> {len(blocks)} blocks "
                f"({len(multi)} need a grouping call; largest {len(blocks[0]) if blocks else 0})")
    model = os.getenv("WSI_DEDUPE_MODEL",
                      "gemini-3-flash-preview" if use_gemini else "gpt-6-luna")
    prompt = PROMPT.read_text(encoding="utf-8")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        grouped = list(ex.map(lambda b: _group_block(b, evidence, prompt, model, use_gemini), multi))
    groups = [g for gs in grouped for g in gs]
    groups += [{"canonical_name": b[0].replace("-", " "), "members": b, "note": ""}
               for b in blocks if len(b) == 1]

    result: Dict[str, CanonicalDataset] = {}
    for g in groups:
        base = slugify(g["canonical_name"]) or g["members"][0]
        slug, n = base, 2
        while slug in result:
            slug, n = f"{base}-{n}", n + 1
        names, descs = set(), []
        for m in g["members"]:
            e = evidence.get(m)
            names |= (e.names if e else {m.replace("-", " ")})
            for d in (e.descriptions if e else []):
                if d not in descs:
                    descs.append(d)
        names.add(g["canonical_name"])
        result[slug] = CanonicalDataset(slug=slug, name=g["canonical_name"],
                                        members=sorted(g["members"]), descriptions=descs[:5],
                                        names=sorted(names), note=g["note"])
    return result


def tcga_parent(name: str) -> Optional[str]:
    """The containing dataset of a TCGA cohort name ("TCGA-KICH" -> "TCGA-RCC",
    "TCGA-RCC" -> "TCGA", "TCGA-LIHC" -> "TCGA"); None for anything else."""
    n = " ".join((name or "").split()).upper()
    m = _TCGA_NAME.match(n) or _TCGA_LONG_NAME.match(n)
    if not m:
        return None
    full = f"TCGA-{m.group(1)}"
    for family, cohorts in TCGA_FAMILIES.items():
        if full in cohorts:
            return family
    return "TCGA"


# CPTAC is a program of cohorts, like TCGA: "CPTAC-LUAD", "cptac ccrcc" and
# "Clinical Proteomic Tumor Analysis Consortium Lung Cancer (CPTAC-LUAD ...)" sit under it.
_CPTAC_NAME = re.compile(r"^CPTAC[-\s]([A-Z0-9]{2,8})$")
_CPTAC_LONG = "CLINICAL PROTEOMIC TUMOR ANALYSIS CONSORTIUM"


def cptac_parent(name: str) -> Optional[str]:
    """"CPTAC" for a CPTAC cohort name; None for anything else (including CPTAC itself)."""
    n = " ".join((name or "").split()).upper()
    if _CPTAC_NAME.match(n) or (n.startswith(_CPTAC_LONG) and len(n) > len(_CPTAC_LONG)):
        return "CPTAC"
    return None


def _parent_overrides() -> Dict[str, str]:
    if not PARENTS_PATH.exists():
        return {}
    import json
    return {k: v for k, v in json.loads(PARENTS_PATH.read_text(encoding="utf-8")).get("parents", {}).items()}


def parent_name(name: str) -> Optional[str]:
    """The dataset `name` is a subset of, by the TCGA/CPTAC rules or the overrides file."""
    over = {k.lower(): v for k, v in _parent_overrides().items()}
    return over.get((name or "").strip().lower()) or tcga_parent(name) or cptac_parent(name)


def contains(parent: str, child: str) -> bool:
    """True if `child` is (transitively) a subset of `parent`."""
    seen, cur = set(), child
    while cur and cur.lower() not in seen:
        seen.add(cur.lower())
        cur = parent_name(cur)
        if cur and cur.lower() == (parent or "").strip().lower():
            return True
    return False


def label_credit(label: str, linked: Iterable[str]) -> Optional[str]:
    """How a human-labelled dataset is covered by the pipeline's links.

    "exact"   -- linked as is
    "implied" -- a part of it is linked (labelled TCGA, linked TCGA-LIHC): the paper uses
                 TCGA because it uses TCGA-LIHC, so this is full credit
    "parent"  -- only a dataset containing it is linked (labelled TCGA-KICH, linked
                 TCGA-RCC): partial credit
    None      -- not covered
    """
    linked = list(linked)
    if label in linked:
        return "exact"
    if any(contains(label, d) for d in linked):
        return "implied"
    if any(contains(d, label) for d in linked):
        return "parent"
    return None


def link_credit(link: str, labelled: Iterable[str]) -> Optional[str]:
    """How a pipeline link is supported by the human labels: "exact"; "specific" (a part
    of a labelled dataset -- linked TCGA-LIHC where the label says TCGA -- counted right);
    "parent" (contains a labelled dataset, partial); None (wrong)."""
    labelled = list(labelled)
    if link in labelled:
        return "exact"
    if any(contains(h, link) for h in labelled):
        return "specific"
    if any(contains(link, h) for h in labelled):
        return "parent"
    return None


def assign_parents(records: Dict[str, dict]) -> Dict[str, str]:
    """Set records[slug]["parent"] to the slug of the record it is a subset of.

    Walks up past parents the catalog lacks: TCGA-KICH goes to TCGA when there is
    no TCGA-RCC entry. Returns child slug -> parent slug.
    """
    by_name = {}
    for slug, rec in records.items():
        for n in [rec.get("name") or slug] + list(rec.get("names") or []):
            by_name.setdefault(" ".join(str(n).split()).lower(), slug)
    out = {}
    for slug, rec in records.items():
        cur = rec.get("name") or slug
        parent = None
        for _ in range(4):
            cur = parent_name(cur)
            if not cur:
                break
            parent = by_name.get(cur.lower())
            if parent and parent != slug:
                break
            parent = None
        rec["parent"] = parent or ""
        if parent:
            out[slug] = parent
    return out


# The 33 TCGA projects on the GDC portal. Added to every catalog as standard entries:
# papers name cohorts (TCGA-KICH) that no extraction happened to surface as a dataset,
# and without an entry the best 2.1 can do is link the parent.
TCGA_PROJECTS = {
    "ACC": "Adrenocortical Carcinoma", "BLCA": "Bladder Urothelial Carcinoma",
    "BRCA": "Breast Invasive Carcinoma",
    "CESC": "Cervical Squamous Cell Carcinoma and Endocervical Adenocarcinoma",
    "CHOL": "Cholangiocarcinoma", "COAD": "Colon Adenocarcinoma",
    "DLBC": "Lymphoid Neoplasm Diffuse Large B-cell Lymphoma", "ESCA": "Esophageal Carcinoma",
    "GBM": "Glioblastoma Multiforme", "HNSC": "Head and Neck Squamous Cell Carcinoma",
    "KICH": "Kidney Chromophobe", "KIRC": "Kidney Renal Clear Cell Carcinoma",
    "KIRP": "Kidney Renal Papillary Cell Carcinoma", "LAML": "Acute Myeloid Leukemia",
    "LGG": "Brain Lower Grade Glioma", "LIHC": "Liver Hepatocellular Carcinoma",
    "LUAD": "Lung Adenocarcinoma", "LUSC": "Lung Squamous Cell Carcinoma", "MESO": "Mesothelioma",
    "OV": "Ovarian Serous Cystadenocarcinoma", "PAAD": "Pancreatic Adenocarcinoma",
    "PCPG": "Pheochromocytoma and Paraganglioma", "PRAD": "Prostate Adenocarcinoma",
    "READ": "Rectum Adenocarcinoma", "SARC": "Sarcoma", "SKCM": "Skin Cutaneous Melanoma",
    "STAD": "Stomach Adenocarcinoma", "TGCT": "Testicular Germ Cell Tumors", "THCA": "Thyroid Carcinoma",
    "THYM": "Thymoma", "UCEC": "Uterine Corpus Endometrial Carcinoma", "UCS": "Uterine Carcinosarcoma",
    "UVM": "Uveal Melanoma",
}
TCGA_FAMILY_NAMES = {"TCGA-RCC": "Renal Cell Carcinoma (KIRC, KIRP, KICH)",
                     "TCGA-NSCLC": "Non-Small Cell Lung Cancer (LUAD, LUSC)",
                     "TCGA-COADREAD": "Colorectal Adenocarcinoma (COAD, READ)",
                     "TCGA-GBM/LGG": "Glioma (GBM, LGG)"}


def standard_dataset_records() -> Dict[str, dict]:
    """Catalog records for every TCGA project, the multi-cohort benchmark families and
    the TCGA umbrella, with the names papers use for them."""
    out = {}
    gdc = "https://portal.gdc.cancer.gov/projects/"
    for code, full in TCGA_PROJECTS.items():
        name = f"TCGA-{code}"
        out[slugify(name)] = {
            "name": name, "members": [slugify(name)], "standard": "tcga",
            "names": sorted({name, f"TCGA {code}", f"TCGA-{code} cohort", f"TCGA {full}",
                             f"The Cancer Genome Atlas {full} (TCGA-{code})", f"The Cancer Genome Atlas {full}"}),
            "descriptions": [f"The {full} cohort of The Cancer Genome Atlas: diagnostic and tissue whole-slide "
                             f"images (H&E) with clinical and genomic data, open access on the GDC portal "
                             f"({gdc}{name})."],
            "note": "Standard TCGA project entry (dataset_canonical.TCGA_PROJECTS)."}
    for name, full in TCGA_FAMILY_NAMES.items():
        out[slugify(name)] = {
            "name": name, "members": [slugify(name)], "standard": "tcga",
            "names": sorted({name, name.replace("-", " "), f"TCGA {full.split(' (')[0]}"}),
            "descriptions": [f"TCGA {full}: the combined whole-slide image cohorts, a standard benchmark."],
            "note": "Standard TCGA benchmark family (dataset_canonical.TCGA_FAMILIES)."}
    out["tcga"] = {"name": "TCGA", "members": ["tcga"], "standard": "tcga",
                   "names": ["TCGA", "The Cancer Genome Atlas", "TCGA Pan-Cancer", "The Cancer Genome Atlas Pan-Cancer"],
                   "descriptions": ["The Cancer Genome Atlas: whole-slide images, clinical and genomic data for 33 "
                                    "cancer types, open access on the GDC portal (https://portal.gdc.cancer.gov/)."],
                   "note": "Standard TCGA umbrella entry."}
    return out


def _tcga_key(name: str) -> Optional[str]:
    n = " ".join((name or "").split()).upper()
    if n in ("TCGA", "THE CANCER GENOME ATLAS"):
        return "TCGA"
    m = _TCGA_NAME.match(n) or _TCGA_LONG_NAME.match(n)
    return f"TCGA-{m.group(1)}" if m else None


def add_standard_datasets(records: Dict[str, dict], canonical_map: Dict[str, str]) -> List[str]:
    """Add the standard TCGA entries, merging into an existing record for the same cohort
    (matched by cohort code, e.g. 'TCGA-LIHC' or '... (TCGA-LIHC)'). Returns added slugs."""
    by_key = {}
    for slug, r in records.items():
        k = _tcga_key(r.get("name") or slug)
        if k:
            by_key.setdefault(k, slug)
    added = []
    for slug, std in standard_dataset_records().items():
        hit = by_key.get(_tcga_key(std["name"]))
        if hit:
            r = records[hit]
            r["names"] = sorted(set(r.get("names") or []) | set(std["names"]))
            r["descriptions"] = list(r.get("descriptions") or []) or std["descriptions"]
            r["standard"] = "tcga"
            continue
        records[slug] = dict(std)
        canonical_map.setdefault(slug, slug)
        added.append(slug)
    return added


MERGES_PATH = Path("pipeline/config/supp/dataset_merges.json")


def apply_manual_merges(records: Dict[str, dict], canonical_map: Dict[str, str]) -> List[str]:
    """Merge records a reviewer decided are one dataset (config/supp/dataset_merges.json).

    Rewrites `records` and `canonical_map` in place; returns the new canonical slugs.
    Slugs that are already merged or absent are skipped, so it is safe to re-apply.
    """
    import json
    if not MERGES_PATH.exists():
        return []
    done = []
    for name, slugs in json.loads(MERGES_PATH.read_text(encoding="utf-8")).get("merges", {}).items():
        parts = [s for s in slugs if s in records]
        if len(parts) < 2:
            continue
        target = slugify(name)
        merged = {"name": name, "members": [], "names": [], "descriptions": [],
                  "note": f"Reviewed merge of {', '.join(parts)} (dataset_merges.json)."}
        for s in parts:
            r = records.pop(s)
            merged["members"] += r.get("members", [])
            merged["names"] += r.get("names", [])
            merged["descriptions"] += [d for d in r.get("descriptions", []) if d not in merged["descriptions"]]
        merged["members"] = sorted(set(merged["members"]))
        merged["names"] = sorted(set(merged["names"]) | {name})
        merged["descriptions"] = merged["descriptions"][:5]
        records[target] = merged
        for m, c in list(canonical_map.items()):
            if c in parts:
                canonical_map[m] = target
        done.append(target)
    return done


def member_map(canon: Dict[str, CanonicalDataset]) -> Dict[str, str]:
    return {m: c.slug for c in canon.values() for m in c.members}


def to_json(canon: Dict[str, CanonicalDataset]) -> dict:
    """Shape written to datasets_seed_deduped.json. `datasets` keeps its old
    meaning (the list of dataset identifiers) so existing readers still work."""
    records = {s: {"name": c.name, "members": c.members, "names": c.names,
                   "descriptions": c.descriptions, "note": c.note}
               for s, c in sorted(canon.items())}
    cmap = member_map(canon)
    apply_manual_merges(records, cmap)
    add_standard_datasets(records, cmap)
    assign_parents(records)
    return {
        "datasets": sorted(records),
        "canonical_map": cmap,
        "records": records,
    }
