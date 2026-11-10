import json
import re
import csv
from pathlib import Path
from typing import List, Dict

from .dedupe import PaperIndex, remove_duplicates, split_seen
from .filters import run_fulltext_filters
from .merge import summarize_and_extract
from .search import logged_searches, run_search, search_workers
import logging
from pipeline.src.utils import atomic_json

logger = logging.getLogger(__name__)



def build_dataset_lists(
    premade_path: str,
    from_papers: List[Dict],
    out: str
) -> List[str]:
    """
    Aggregates dataset names from a premade list and from processed papers.

    Args:
        premade_path: Path to a text file with known dataset names.
        from_papers: List of enriched PaperRecord dictionaries.
        out: Output path for the final JSON file.

    Returns:
        A sorted list of unique dataset names.
    """
    def _slugify(name: str) -> str:
        s = (name or "").lower()
        s = re.sub(r"[^a-z0-9]+", "-", s)
        s = re.sub(r"-+", "-", s).strip('-')
        return s

    dataset_slugs = set()

    # 1. Load Premade List (supports .txt or .csv)
    try:
        p = Path(premade_path)
        if p.suffix.lower() == '.csv':
            with open(p, newline='', encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    raw = (row.get('slug') or row.get('dataset_name') or '').strip()
                    if raw:
                        slug = _slugify(raw)
                        if slug:
                            dataset_slugs.add(slug)
        else:
            with open(p, 'r', encoding="utf-8") as f:
                for line in f:
                    name = line.strip()
                    if name:
                        slug = _slugify(name)
                        if slug:
                            dataset_slugs.add(slug)
    except FileNotFoundError:
        pass

    # 1b. Optional additional public dataset list
    public_list_path = Path("pipeline/config/supp/public_dataset_list.txt")
    try:
        with open(public_list_path, 'r', encoding="utf-8") as f:
            for line in f:
                name = line.strip()
                if name:
                    slug = _slugify(name)
                    if slug:
                        dataset_slugs.add(slug)
    except FileNotFoundError:
        pass

    # 2a. Extract Datasets from Papers (older summary prompt output)
    for paper in from_papers:
        # Using .get() to avoid KeyErrors if keys are missing
        extraction_data = paper.get('extraction', {}).get('sum_paper.md', {})
        if extraction_data:
            datasets_used = extraction_data.get("Datasets Used", [])
            for dataset in datasets_used:
                name = (dataset.get("Name") or "").strip()
                if name:
                    slug = _slugify(name)
                    if slug:
                        dataset_slugs.add(slug)

    # 2b. Extract Datasets from new dataset_details.md (schema-aligned, prefer public)
    for paper in from_papers:
        ds_block = paper.get('extraction', {}).get('dataset_details.md', {}) or {}
        for ds in ds_block.get('datasets', []) or []:
            try:
                is_public = bool(ds.get('is_public', True))
            except Exception:
                is_public = True
            if not is_public:
                continue
            # Require at least one usable dataset URL that is not a publisher site
            code_url = (ds.get('code_url') or '').strip()
            download_url = (ds.get('download_url') or '').strip()
            home_page_url = (ds.get('home_page_url') or '').strip()
            
            # Check if at least one URL exists
            if not any([code_url, download_url, home_page_url]):
                continue
            
            # Check all URLs against publisher blacklist
            publisher_blacklist = (
                'ieeexplore.ieee.org', 'nature.com', 'frontiersin.org',
                'sciencedirect.com', 'springer.com', 'link.springer.com', 'elsevier.com'
            )
            all_urls = [code_url, download_url, home_page_url]
            valid_urls = [url for url in all_urls if url]
            
            # Skip if all valid URLs are publisher sites
            if all(any(host in url.lower() for host in publisher_blacklist) for url in valid_urls):
                continue
            name = (ds.get('dataset_name') or ds.get('slug') or '').strip()
            if name:
                slug = _slugify(name)
                if slug:
                    dataset_slugs.add(slug)

    # 3. Combine and Finalize
    final_list = sorted(list(dataset_slugs))

    atomic_json(out, {"datasets": final_list}, indent=4)

    return final_list


def _parallel_search_terms(queries: List[str]) -> List[Dict]:
    """run_search(term=...) for each query, in WSI_SEARCH_WORKERS threads (the work waits
    on HTTP), checkpointed per query; results in query order."""
    if not queries:
        return []
    per_query = logged_searches("1.6_search", [{"term": q} for q in queries],
                                lambda call: run_search(term=call["term"]),
                                threads=search_workers(), desc="Dataset searches")
    return [r for results in per_query for r in results]


def search_papers_for_datasets(datasets_seed: list[str]) -> list[dict]:
    """
    Performs a targeted paper search for each dataset in the seed list.

    Args:
        datasets_seed: A list of dataset names.

    Returns:
        A list of PaperRecord dictionaries from the new search.
    """
    queries = [f'"{dataset_name}" histopathology WSI' for dataset_name in datasets_seed]
    return _parallel_search_terms(queries)


def run_1_1_to_1_4(
    seed_results: list[dict], 
    prompts_dir: Path, 
    out: Path, 
    use_gemini: bool = False,
    already_processed: list[dict] = None,
    use_batch: bool = False,
    use_browser: bool = False,
) -> list[dict]:
    """
    Runs the core paper processing pipeline (steps 1.1 to 1.4) on a list of seed results.

    Args:
        seed_results: The initial list of papers from a search.
        prompts_dir: The path to the prompts directory.
        out: The final output path for the processed papers.
        use_gemini: If True, use Gemini API instead of OpenAI.
        already_processed: Optional list of papers already processed in previous steps.
                          Papers matching these will be filtered out.
        use_batch: If True, use batch API for LLM calls.

    Returns:
        A fully processed and enriched list of papers.
    """
    # Step 1.1 (Implicit): Initial search is done prior to calling this function
    # Step 1.2: Citation search is skipped in this focused run

    # Remove duplicates from the initial seed
    unique_papers = remove_duplicates(seed_results)
    
    # Remove papers that have already been processed in previous steps
    out = Path(out)
    if already_processed:
        initial_count = len(unique_papers)
        unique_papers, seen = split_seen(unique_papers, PaperIndex(already_processed))
        atomic_json(out.with_name(out.stem + "_already_seen.json"), seen, indent=1)
        removed_count = initial_count - len(unique_papers)
        if removed_count > 0:
            print(f"Filtered out {removed_count} papers already processed in previous steps.")

    # Prepare checkpoint paths
    fulltext_path = out.with_name(out.stem + "_fulltext.json")

    def _checkpointed(label: str, path: Path, run):
        """Reuse a step's checkpoint if present, else run it, and report which happened."""
        print("")
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                print(f"{label}: reusing checkpoint {path.name} ({len(data)} papers) — no LLM calls.")
                return data
            except Exception as e:
                print(f"{label}: checkpoint {path.name} unreadable ({e}); recomputing.")
        print(f"{label}: running...")
        return run()

    # Step 1.6b: Full-text filtering
    fulltext_papers = _checkpointed(
        "Step 1.6b: Full-text filtering",
        fulltext_path,
        lambda: run_fulltext_filters(
            items=unique_papers,
            prompt_path=str(prompts_dir / "check_all_criteria_structured.md"),
            out=str(fulltext_path),
            use_gemini=use_gemini,
            use_batch=use_batch,
            use_browser=use_browser,
            step_name="fulltext_filter_1_6b",
        ),
    )

    # Step 1.6c: Summarize and extract from final papers
    prompts_to_run = [
        "sum_paper.md",
        "paper_classification_prompt.md",
        "dataset_details.md",
    ]
    enriched_papers = _checkpointed(
        "Step 1.6c: Summarization and extraction for dataset-focused papers",
        out,
        lambda: summarize_and_extract(
            items=fulltext_papers,
            prompts=prompts_to_run,
            prompts_dir=prompts_dir,
            out=out,
            use_gemini=use_gemini,
            use_batch=use_batch,
        ),
    )

    # Save the final list
    atomic_json(out, enriched_papers, indent=4)

    return enriched_papers
