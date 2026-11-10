"""Module for matching papers to datasets."""
import re
import json
import logging
import os
from pathlib import Path
from typing import List, Dict, Set, Optional
from pipeline.utils.llms.llm import run_gpt, strip_json_wrapper, run_batch_requests, _extract_batch_content
from pipeline.src.paper_code.dedupe import normalize_slug
from pipeline.src.dataset_code.dataset_alias import (
    build_alias_catalog,
    build_paper_text,
    prefilter_match,
)

logger = logging.getLogger(__name__)


def _harvest_extraction_names_by_slug(papers: List[Dict]) -> Dict[str, Set[str]]:
    """Group the free-text dataset names each paper's extraction wrote, by slug.

    These are high-quality alias seeds -- the heavy model's own naming, often in
    the "Full Name (ACRONYM)" form -- shared across the whole pool.
    """
    out: Dict[str, Set[str]] = {}
    for paper in papers:
        dd = (paper.get('extraction') or {}).get('dataset_details.md')
        if not isinstance(dd, dict):
            continue
        for ds in dd.get('datasets', []) or []:
            if not isinstance(ds, dict):
                continue
            name = ds.get('dataset_name') or ds.get('name')
            slug = ds.get('slug') or (normalize_slug(name) if name else '')
            slug = normalize_slug(slug)
            if slug and isinstance(name, str) and name.strip():
                out.setdefault(slug, set()).add(name.strip())
    return out


def _load_rich_catalog_by_slug() -> Dict[str, dict]:
    """Best-effort ``normalized_slug -> {dataset_name, description}`` from any
    prior catalog artifacts. Absent files are fine -- aliases still derive from
    the pool string and per-paper extractions."""
    rich: Dict[str, dict] = {}
    seed = Path('pipeline/data/dataset_data/datasets_seed_step2.json')
    try:
        if seed.exists():
            data = json.loads(seed.read_text(encoding="utf-8"))
            records = data.get('datasets', data) if isinstance(data, dict) else data
            for rec in records or []:
                if not isinstance(rec, dict):
                    continue
                slug = normalize_slug(rec.get('slug') or rec.get('dataset_name') or '')
                if slug:
                    rich.setdefault(slug, rec)
    except Exception as e:
        logger.warning(f"Could not load rich dataset catalog: {e}")
    return rich


def generate_paper_dataset_matches(
    papers: List[Dict],
    prompt_path: Path,
    output_path: Path,
    max_papers: int = None,
    use_gemini: bool = False,
    dataset_names: List[str] | None = None,
    use_batch: bool = False,
    alias_names: Dict[str, Set[str]] | None = None,
) -> List[Dict]:
    """
    Generate paper-dataset matches (step 2.1) with ``match_via_extraction``.

    Args:
        papers: List of paper dicts
        prompt_path: Path to map_extracted_datasets.md
        output_path: Path to save matches JSONL
        max_papers: Optional limit on number of papers to process
        use_gemini: Whether to use Gemini for the mapping calls
        dataset_names: The dataset pool to match against
        use_batch: Whether to use batch API for LLM calls
        alias_names: Optional ``pool name -> variant names`` from 1.5b
            canonicalization, so the prefilter recognises every name a dataset
            was written under, not just its canonical one.

    Returns:
        List of match dicts with paper_id, dataset_name, score, rationale
    """
    if max_papers:
        papers = papers[:max_papers]

    # Check if output already exists and load existing matches
    existing_matches = {}
    if output_path.exists():
        logger.info(f"Loading existing matches from {output_path}")
        with open(output_path, 'r', encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    match = json.loads(line)
                    key = (match.get('paper_id'), match.get('dataset_name'))
                    existing_matches[key] = match
        logger.info(f"Loaded {len(existing_matches)} existing matches")

    # Map each paper's own extracted dataset list onto its candidates, one call per paper.
    return match_via_extraction(
        papers, dataset_names or [], output_path,
        prompt_path=prompt_path,
        use_gemini=use_gemini, use_batch=use_batch, alias_names=alias_names,
        existing_matches=existing_matches,
    )


_TCGA_TOKEN = re.compile(r"(?<![A-Za-z0-9])TCGA(?:-[A-Z]{2,5})?(?![A-Za-z0-9])")


def _paper_excerpts(paper: Dict, names: List[str], window: int = 200, limit: int = 6) -> List[str]:
    """Short passages around TCGA cohort codes and the extracted dataset names --
    the evidence the mapping call needs to pick the right cohort."""
    text = paper.get('fulltext') or ''
    if not text:
        return []
    spans = [m.start() for m in _TCGA_TOKEN.finditer(text)][:limit // 2]
    lower = text.lower()
    for n in names:
        i = lower.find(n.lower()) if len(n) >= 3 else -1
        if i >= 0:
            spans.append(i)
    out, taken = [], []
    for s in sorted(spans):
        if any(abs(s - t) < window for t in taken):
            continue
        taken.append(s)
        out.append(' '.join(text[max(0, s - window):s + window].split()))
        if len(out) == limit:
            break
    return out


def _extracted_datasets(paper: Dict) -> List[Dict]:
    dd = (paper.get('extraction') or {}).get('dataset_details.md') or {}
    return [d for d in dd.get('datasets') or []
            if isinstance(d, dict) and (d.get('dataset_name') or d.get('name'))]


_REFS_WORD = re.compile(r"(?<![A-Za-z])(References|REFERENCES|Bibliography|Literature Cited)(?![A-Za-z])")
_YEAR = re.compile(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)")


def _reference_list_start(text: str) -> int:
    """Where the paper's reference list starts, or len(text) when none is found.

    Found by what follows the heading -- a run of citation years -- not by position:
    a publisher landing page is an abstract and then mostly references, so the list
    can start early, while a "References" link in page chrome is followed by prose.
    """
    starts = [m.start() for m in _REFS_WORD.finditer(text or "")]
    for at in reversed(starts):
        if at >= 500 and len(_YEAR.findall(text[at:at + 1500])) >= 5:
            return at
    return len(text or "")


def _named_only_in_references(dataset: Dict, text: str) -> bool:
    """The dataset's name appears in the text, but only in the reference list -- a cited
    dataset, not one the paper used. Matched like the prefilter (acronym, full form, or
    all core words, so word order and year spelling don't matter). A name that appears
    nowhere is kept: extractions often write a canonical name the text spells otherwise."""
    names = {str(dataset.get(k) or "").strip() for k in ("dataset_name", "name")} - {""}
    if not names or not text:
        return False
    start = _reference_list_start(text)
    if start >= len(text):
        return False
    alias = build_alias_catalog(["d"], extraction_names_by_slug={"d": names})["d"]
    if prefilter_match(build_paper_text({"fulltext": text[:start]}), alias)[0]:
        return False
    # In the reference list the whole name must sit within one citation: "TCGA" in one
    # reference and "NSCLC" in another is not a mention of TCGA-NSCLC.
    refs = text[start:]
    return any(prefilter_match(build_paper_text({"fulltext": refs[i:i + 300]}), alias)[0]
               for i in range(0, max(1, len(refs)), 150))


def _used_datasets(paper: Dict) -> List[Dict]:
    """The extraction's datasets, minus those the paper names only in its references."""
    text = paper.get('fulltext') or ''
    return [d for d in _extracted_datasets(paper) if not _named_only_in_references(d, text)]


def _mapping_user_prompt(paper: Dict, candidates: List[str]) -> Optional[str]:
    listed = _used_datasets(paper)
    if not candidates or not listed:
        return None
    names = [d.get('dataset_name') or d.get('name') for d in listed]
    lines = [f"- {d.get('dataset_name') or d.get('name')}: "
             f"{' '.join(str(d.get('description') or '').split())[:200]}" for d in listed]
    excerpts = _paper_excerpts(paper, names)
    entries = [f"{i}: {c}" for i, c in enumerate(candidates)]
    return (f"Paper: {paper.get('title', '')}\n\n"
            f"Datasets this paper reports using (from its extraction):\n" + ("\n".join(lines) or "(none listed)")
            + ("\n\nPaper excerpts:\n" + "\n".join(f"> {e}" for e in excerpts) if excerpts else "")
            + "\n\nCatalog entries (with passages where each one's own name appears):\n" + "\n".join(entries))


def _parse_mapping(content: Optional[str], candidates: List[str]) -> Set[str]:
    try:
        parsed = json.loads(strip_json_wrapper(content or '') or '{}')
    except (json.JSONDecodeError, TypeError):
        return set()
    picked = set()
    for k in (parsed or {}).get('catalog_ids') or []:
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(candidates):
            picked.add(candidates[idx])
    return picked


def match_via_extraction(
    papers: List[Dict],
    dataset_names: List[str],
    output_path: Path,
    prompt_path: Path,
    use_gemini: bool = False,
    use_batch: bool = False,
    alias_names: Dict[str, Set[str]] | None = None,
    existing_matches: Dict | None = None,
    workers: int = 32,
) -> List[Dict]:
    """Step 2.1: one call per paper.

    The heavy extraction already lists the datasets each paper uses. The lexical
    prefilter picks the paper's candidate catalog entries, and a single call maps
    the extracted list onto them.
    """
    existing_matches = existing_matches or {}
    done_papers = {pid for pid, _ in existing_matches}
    names_by_slug = _harvest_extraction_names_by_slug(papers)
    for pool_name, variants in (alias_names or {}).items():
        names_by_slug.setdefault(normalize_slug(pool_name), set()).update(variants)
    catalog = build_alias_catalog(dataset_names, rich_by_slug=_load_rich_catalog_by_slug(),
                                  extraction_names_by_slug=names_by_slug)

    jobs = []  # (paper, paper_id, candidates, user_prompt)
    for paper in papers:
        paper_id = paper.get('link') or paper.get('url') or paper.get('title')
        if paper_id in done_papers:
            continue
        text = build_paper_text(paper)
        candidates = [d for d, alias in catalog.items() if prefilter_match(text, alias)[0]]
        user_prompt = _mapping_user_prompt(paper, candidates)
        if user_prompt:
            jobs.append((paper, paper_id, candidates, user_prompt))
    logger.info(f"Extraction-route matching: {len(jobs)} papers with extracted datasets "
                f"and catalog candidates (1 call each)")

    system_prompt = Path(prompt_path).read_text(encoding="utf-8")
    model = os.getenv('WSI_MATCH_MODEL',
                      'gemini-3-flash-preview' if use_gemini else 'gpt-6-luna')
    if use_batch and jobs:
        requests = [{'custom_id': f'map:{i}', 'system_prompt': system_prompt, 'user_prompt': up,
                     'model': model, 'response_format': {'type': 'json_object'}}
                    for i, (_, _, _, up) in enumerate(jobs)]
        results = run_batch_requests(requests=requests, step_name='paper_dataset_map',
                                     use_gemini=use_gemini, model=model)
        contents = [_extract_batch_content(results.get(f'map:{i}'), use_gemini) for i in range(len(jobs))]
    else:
        def _one(job):
            try:
                # An empty mapping ({"catalog_ids": []}) is a valid answer, not a failure.
                resp = run_gpt(system_prompt=system_prompt, user_prompt=job[3], model=model,
                               response_format={'type': 'json_object'}, use_gemini=use_gemini,
                               reject_all_empty=False, item=str(job[1]))
                return {"content": resp.choices[0].message.content}
            except Exception as e:  # noqa: BLE001 - one paper failing must not stop the step
                logger.warning(f"Mapping call failed for {job[1]}: {e}")
                from pipeline.src.checkpoint import is_permanent_error
                return {"content": None, "call_error": str(e)[:300]} if is_permanent_error(e) else {"error": str(e)[:300]}
        # Checkpointed per paper (and per candidate list, which the catalog decides); a
        # failed call is retried, then left unmatched as before.
        from pipeline.src.checkpoint import fingerprint, run_items, thread_runner
        job_key = lambda job: f"{job[1]}|{fingerprint(job[2], job[3])}"  # noqa: E731
        done, _ = run_items("2.1_match", jobs, key=job_key, fn=_one,
                            runner=thread_runner(workers, "Mapping extracted datasets"),
                            failed=lambda res: "error" in res, fp=fingerprint(system_prompt, model))
        contents = [(done.get(job_key(job)) or {}).get("content") for job in jobs]

    matches = []
    with open(output_path, 'a', encoding="utf-8") as f:
        for (paper, paper_id, candidates, _), content in zip(jobs, contents):
            names = [d.get('dataset_name') or d.get('name') for d in _extracted_datasets(paper)]
            for dataset_name in sorted(_parse_mapping(content, candidates)):
                match = {'paper_id': paper_id, 'paper_title': paper.get('title', ''),
                         'dataset_name': dataset_name, 'score': 0.9,
                         'rationale': f"mapped from extraction: {'; '.join(names)[:300]}",
                         'is_match': True, 'method': 'extraction'}
                matches.append(match)
                f.write(json.dumps(match) + '\n')
    matches.extend(existing_matches.values())
    logger.info(f"Generated {len(matches)} total paper-dataset matches")
    return matches


def get_unique_datasets_from_matches(matches: List[Dict], max_datasets: int = None) -> List[str]:
    """
    Extract unique dataset names from matches.
    
    Args:
        matches: List of match dicts
        max_datasets: Optional limit on number of datasets
        
    Returns:
        List of unique dataset names, sorted by frequency
    """
    from collections import Counter
    
    dataset_counts = Counter(m['dataset_name'] for m in matches if m.get('is_match'))
    
    # Sort by frequency (most common first)
    sorted_datasets = [name for name, count in dataset_counts.most_common()]
    
    if max_datasets:
        sorted_datasets = sorted_datasets[:max_datasets]
    
    logger.info(f"Extracted {len(sorted_datasets)} unique datasets from matches")
    return sorted_datasets
