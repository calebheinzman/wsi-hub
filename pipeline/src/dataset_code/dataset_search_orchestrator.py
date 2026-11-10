"""Main orchestrator for dataset search pipeline with multiprocessing support."""
import json
import time
import os
import logging
from pathlib import Path
from typing import List, Dict, Optional
from multiprocessing import Pool
from tqdm import tqdm

from .search_providers.serper import search_serper, build_dataset_queries
from .search_providers.perplexity import search_perplexity
from .search_providers.llm_search import search_gpt, search_gemini
from .dataset_canonical import safe_dir_name
from .page_processor import fetch_and_save_pages, keep_page, page_url, summarize_all_pages
from .dataset_validator import (
    merge_search_results,
    validate_pathology_dataset,
    extract_dataset_metadata,
    normalize_metadata,
    find_download_link,
    validation_call_failed,
)

from pipeline.src.utils import atomic_json, pool_size, replace_retry
from pipeline.utils.llms.llm import ResponseNotSaved

logger = logging.getLogger(__name__)


def _is_insufficient_content(metadata: Optional[Dict]) -> bool:
    if not metadata:
        return True
    try:
        dataset_name = (metadata.get("dataset_name") or "").strip()
        description = (metadata.get("description") or "").strip()
        year = metadata.get("year")
        year_int = int(year) if isinstance(year, int) else int(float(year)) if year is not None else 0
        urls = [
            (metadata.get("download_url") or "").strip(),
            (metadata.get("home_page_url") or "").strip(),
            (metadata.get("code_url") or "").strip(),
        ]
        has_any_url = any(bool(u) for u in urls)

        if (not dataset_name) and (not description) and (year_int == 0) and (not has_any_url):
            return True
    except Exception:
        return False
    return False


def with_paper_descriptions(merged_results: Dict, descriptions: List[str]) -> Dict:
    """Put the papers' own descriptions of the dataset in front of the search text.

    Search results for a name can be about another release of the same program:
    for TCGA-KIRC the top hit is TCIA's radiology collection, while the papers use
    the cohort's whole-slide images. The descriptions tell validation, extraction
    and the download-link step which dataset is meant. Not written to disk.
    """
    descs = [" ".join(str(d).split()) for d in (descriptions or []) if str(d).strip()][:3]
    if not descs or not merged_results:
        return merged_results
    out = dict(merged_results)
    out['paper_descriptions'] = descs
    out['combined_text'] = (
        "HOW RESEARCH PAPERS THAT USE THIS DATASET DESCRIBE IT -- this is the dataset to judge and "
        "describe. Ignore search results about a different dataset or a different release (for "
        "example a radiology collection of the same cohort):\n"
        + "\n".join(f"- {d}" for d in descs)
        + "\n\n=== SEARCH RESULTS ===\n" + (merged_results.get('combined_text') or ''))
    return out


def _read_cache(path: Path) -> Optional[Dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, ValueError):
        return None


def _llm_search_failed(llm_results: Dict) -> bool:
    """A cached LLM search in which every provider errored or returned nothing."""
    entries = [v for v in (llm_results or {}).values() if isinstance(v, dict)]
    return not entries or all(e.get('error') or not (e.get('content') or '').strip() for e in entries)


def _extraction_call_failed(meta: Optional[Dict]) -> bool:
    """A cached metadata extraction that is a failed call, not an extraction."""
    return (not meta or bool(meta.get("error"))
            or meta.get("validation_error") == "Batch extraction returned None")


def _link_lookup(merged_results: Dict, dataset_name: str, download_link_path: Path, prompts_dir: Path,
                 use_gemini: bool) -> Dict:
    """download_link.json, re-asked when missing or when the cached lookup was a failed call.
    A failed call is not cached, so the next run asks again."""
    cached = _read_cache(download_link_path)
    if cached is not None and cached.get("status") != "error":
        return cached
    info = find_download_link(merged_results, dataset_name, prompts_dir / "download_link.md", use_gemini,
                              entity_type='dataset')
    if info.get("status") != "error":
        download_link_path.write_text(json.dumps(info, indent=2), encoding="utf-8")
    return info


def _resolve_public(meta: Dict, dataset_dir: Path) -> None:
    """Let validation's public verdict stand when extraction could not tell.

    Extraction answering is_public=false with dataset_access unknown means it found
    no access information, not a restriction -- SPI-2M (on Hugging Face) was
    rejected this way. Only an explicit restriction, or validation agreeing, rejects.
    """
    if not meta or meta.get('is_public'):
        return
    if str(meta.get('dataset_access') or '').strip().lower() not in ('', 'unknown'):
        return
    vpath = dataset_dir / "validation.json"
    try:
        validation = json.loads(vpath.read_text(encoding="utf-8")) if vpath.exists() else {}
    except (OSError, ValueError):
        return
    if validation.get('appears_public') is True:
        meta['is_public'] = True
        meta['public_source'] = 'validation'


VERDICTS = ("merged_search.json", "validation.json", "metadata_validated.json", "download_link.json",
            "metadata_merged.json")


def _set_aside(dataset_dir: Path) -> None:
    """A changed search makes the dataset's pages and the verdicts drawn from them stale.
    Page files are named by rank + domain, so a re-ordered result list would silently
    reuse an old page for a different URL at the same rank. They go to
    superseded/<time>/, each page archived whole first (page_processor.PAGES_INDEX), and
    the verdicts are made again from the new pages. Never deletes: a file that can't be
    moved is left where it is and the dataset errors out of this run, to be retried."""
    pages_dir = dataset_dir / "pages"
    pages = sorted(pages_dir.glob("*.txt")) if pages_dir.exists() else []
    stale = pages + (sorted(pages_dir.glob("*.summary.json")) if pages_dir.exists() else []) + [
        dataset_dir / n for n in VERDICTS if (dataset_dir / n).exists()]
    if not stale:
        return
    for page in pages:
        keep_page(page, page_url(page), "superseded")
    stamp, n = time.strftime("%Y%m%dT%H%M%S"), 0
    aside = dataset_dir / "superseded" / stamp
    while aside.exists():  # a second change within the second
        n += 1
        aside = dataset_dir / "superseded" / f"{stamp}-{n}"
    aside.mkdir(parents=True)
    for f in stale:
        for wait in (0, 0.5, 2):
            time.sleep(wait)
            try:
                os.replace(f, aside / f.name)  # os.replace never deletes its source on failure
                break
            except OSError:
                pass
        else:
            raise OSError(f"could not set aside {f}; left in place")


NOT_SAVED = "_response_not_saved"


def _process_in_worker(args: tuple) -> Dict:
    """process_single_dataset in a multiprocessing.Pool worker. A BaseException there kills
    the worker and leaves the pool waiting for its task forever, so ResponseNotSaved comes
    back as a marker and the parent raises it (run_dataset_search_pipeline)."""
    try:
        return process_single_dataset(args)
    except ResponseNotSaved as e:
        return {NOT_SAVED: str(e)}


def process_single_dataset(args: tuple) -> Dict:
    """
    Process a single dataset (designed for multiprocessing).
    
    Args:
        args: Tuple of (dataset_name, config_dict)
        
    Returns:
        Result dict with status and paths
    """
    dataset_name, config = args
    
    base_dir = Path(config['base_dir'])
    prompts_dir = Path(config['prompts_dir'])
    schema_path = Path(config['schema_path'])
    use_gemini = config.get('use_gemini', False)
    max_pages = config.get('max_pages', 10)
    use_perplexity = config.get('use_perplexity', False)
    use_browser = config.get('use_browser', False)
    use_batch = config.get('use_batch', False)
    
    # Create dataset directory under processed/
    slug = dataset_name.lower().replace(' ', '-').replace('_', '-')
    dataset_dir = base_dir / 'processed' / safe_dir_name(slug)
    dataset_dir.mkdir(parents=True, exist_ok=True)
    
    result = {
        "dataset_name": dataset_name,
        "slug": slug,
        "status": "started",
        "dataset_dir": dataset_dir.as_posix()
    }
    
    try:
        # Step 1: Search providers
        search_dir = dataset_dir / "search"
        search_dir.mkdir(exist_ok=True)
        
        serper_results_path = search_dir / "serper_results.json"
        llm_results_path = search_dir / "llm_results.json"
        
        # Serper search: every query in build_dataset_queries, merged without duplicate
        # links. The cache records which queries it holds, so a cache made before a
        # query was added runs only the new one.
        serper_results = json.loads(serper_results_path.read_text(encoding="utf-8")) if serper_results_path.exists() else []
        queries_path = search_dir / "serper_queries.json"
        done = json.loads(queries_path.read_text(encoding="utf-8")) if queries_path.exists() else (
            # legacy caches hold the topical query's results -- unless the search failed and
            # the cache is empty (January 2026: 312 datasets), which is no search at all
            [build_dataset_queries(dataset_name)[-1]] if serper_results else [])
        search_changed = False
        for query in build_dataset_queries(dataset_name):
            if query in done:
                continue
            new = search_serper(query, num_results=10)
            if not new:
                continue  # a failed search is retried next time, not recorded as done
            seen = {r.get('link') for r in serper_results}
            # Exact-name results go first: fetch order prefers them within a domain tier.
            serper_results = [r for r in new if r.get('link') not in seen] + serper_results
            done.append(query)
            search_changed = True
        if search_changed:
            # Before the new results are saved, so a stop in between can't leave the old
            # verdicts standing beside the new search (_set_aside raises rather than lose one).
            _set_aside(dataset_dir)
        if search_changed or not serper_results_path.exists():
            serper_results_path.write_text(json.dumps(serper_results, indent=2), encoding="utf-8")
            queries_path.write_text(json.dumps(done, indent=2), encoding="utf-8")
        
        # LLM searches (a cached search whose every provider failed is searched again)
        cached_llm = _read_cache(llm_results_path)
        llm_changed = cached_llm is None or _llm_search_failed(cached_llm)
        if llm_changed:
            llm_results = {}
            
            # Perplexity
            if use_perplexity:
                llm_results['perplexity'] = search_perplexity(dataset_name)
            
            # GPT or Gemini
            if use_gemini:
                llm_results['gemini'] = search_gemini(dataset_name)
            else:
                llm_results['gpt'] = search_gpt(dataset_name)
            
            atomic_json(llm_results_path, llm_results, indent=2)
        else:
            with open(llm_results_path, 'r', encoding="utf-8") as f:
                llm_results = json.load(f)
        
        # Step 2: Fetch and summarize pages
        pages_dir = dataset_dir / "pages"
        pages_dir.mkdir(exist_ok=True)

        # Fetch pages
        page_paths = fetch_and_save_pages(serper_results, pages_dir, max_pages=max_pages, use_browser=use_browser)
        
        # Summarize pages
        sum_page_prompt = prompts_dir / "sum_page.md"
        summaries = summarize_all_pages(page_paths, dataset_name, sum_page_prompt, use_gemini, entity_type='dataset')
        
        # Step 3: Merge results
        merged_path = dataset_dir / "merged_search.json"
        cached_merged = _read_cache(merged_path)
        if search_changed or llm_changed or not (cached_merged or {}).get('combined_text'):
            merged_results = merge_search_results(serper_results, llm_results, summaries)
            # Not cached when a page summary failed, so the next run summarizes it again.
            if not any(sm.get('error') for sm in summaries):
                atomic_json(merged_path, merged_results, indent=2)
        else:
            merged_results = cached_merged
        
        merged_results = with_paper_descriptions(
            merged_results, (config.get('descriptions') or {}).get(dataset_name, []))

        # Step 4: Validate pathology dataset. A failed call is neither a rejection nor
        # cached: the dataset errors out of this run and is validated again next run.
        validation_path = dataset_dir / "validation.json"
        validation = _read_cache(validation_path)
        if validation_call_failed(validation):
            validation_prompt = prompts_dir / "validate_pathology_dataset.md"
            validation = validate_pathology_dataset(
                merged_results, dataset_name, validation_prompt, use_gemini, entity_type='dataset'
            )
            if validation_call_failed(validation):
                result['status'] = 'error'
                result['reason'] = 'validation_call_failed'
                result['error'] = validation.get('error', '')
                return result
            atomic_json(validation_path, validation, indent=2)
        
        # Skip if not a pathology dataset
        if not validation.get('is_pathology_dataset', False):
            result['status'] = 'rejected'
            result['reason'] = validation.get('disqualifier', 'not_pathology')
            return result

        # Enforce publicness at validation stage
        if not validation.get('appears_public', False):
            result['status'] = 'rejected'
            result['reason'] = 'not_public_validation'
            return result
        
        # Step 5 & 6: Extract metadata (will be done in batch mode after all datasets are processed)
        metadata_validated_path = dataset_dir / "metadata_validated.json"
        
        # Check if metadata already exists from previous run (a cached failed call doesn't count)
        metadata_validated = _read_cache(metadata_validated_path)
        if _extraction_call_failed(metadata_validated):
            # Mark as pending batch extraction
            result['status'] = 'pending_extraction'
            result['merged_results'] = merged_results
            return result

        # Enforce publicness at extraction stage
        _resolve_public(metadata_validated, Path(dataset_dir))
        if not metadata_validated.get('is_public', False):
            if _is_insufficient_content(metadata_validated):
                result['status'] = 'error'
                result['reason'] = 'insufficient_content'
                result['error'] = 'Insufficient content to determine publicness (empty/placeholder metadata)'
                return result
            result['status'] = 'rejected'
            result['reason'] = 'not_public_extraction'
            return result
        
        # Step 7: Find download link
        download_info = _link_lookup(merged_results, dataset_name, dataset_dir / "download_link.json",
                                     prompts_dir, use_gemini)
        
        # Step 8: Write merged metadata (validated metadata + download link)
        merged_meta_path = dataset_dir / "metadata_merged.json"
        if not merged_meta_path.exists():
            merged_meta = dict(metadata_validated)
            merged_meta["dataset_name"] = metadata_validated.get("dataset_name", dataset_name)
            merged_meta["slug"] = slug
            merged_meta["code_url"] = merged_meta.get("code_url") or download_info.get("code_url", "")
            merged_meta["download_url"] = merged_meta.get("download_url") or download_info.get("download_url", "")
            merged_meta["home_page_url"] = merged_meta.get("home_page_url") or download_info.get("home_page_url", "")
            merged_meta["download_status"] = download_info.get("status", "")
            # Carry validation status from earlier steps as a unified flag
            merged_meta["is_pathology_dataset"] = True
            atomic_json(merged_meta_path, merged_meta, indent=2)
        else:
            with open(merged_meta_path, 'r', encoding="utf-8") as f:
                merged_meta = json.load(f)

        # Update result
        result['status'] = 'completed'
        result['is_public'] = metadata_validated.get('is_public', False)
        result['validation_passed'] = metadata_validated.get('validation_passed', False)
        result['code_url'] = download_info.get('code_url', '')
        result['download_url'] = download_info.get('download_url', '')
        result['home_page_url'] = download_info.get('home_page_url', '')
        
        return result
        
    except Exception as e:
        logger.error(f"Error processing dataset {dataset_name}: {e}")
        result['status'] = 'error'
        result['error'] = str(e)
        return result


def run_batch_metadata_extraction(
    results: List[Dict],
    base_dir: Path,
    prompts_dir: Path,
    schema_path: Path,
    use_gemini: bool = False
) -> List[Dict]:
    """
    Run batch metadata extraction for all datasets that need it.
    
    This aggregates all pending datasets and runs them through batch extraction
    together, with automatic retries for failed items.
    
    Args:
        results: List of dataset processing results
        base_dir: Base directory for dataset data
        prompts_dir: Directory containing prompts
        schema_path: Path to dataset schema
        use_gemini: Whether to use Gemini
        
    Returns:
        Updated list of results with extraction completed
    """
    from .dataset_validator import batch_extract_dataset_metadata
    
    # Collect datasets that need extraction
    pending_items = []
    pending_indices = []
    
    for idx, result in enumerate(results):
        if result.get('status') == 'pending_extraction':
            dataset_name = result['dataset_name']
            merged_results = result.get('merged_results')
            if merged_results:
                pending_items.append((dataset_name, merged_results))
                pending_indices.append(idx)
    
    if not pending_items:
        logger.info("No datasets pending extraction")
        return results
    
    logger.info(f"Running batch extraction for {len(pending_items)} datasets")
    
    # Run batch extraction with validation and retries
    ex_data_prompt = prompts_dir / "ex_data.md"
    extracted_results = batch_extract_dataset_metadata(
        items=pending_items,
        prompt_path=ex_data_prompt,
        schema_path=schema_path,
        use_gemini=use_gemini,
        entity_type='dataset'
    )
    
    # Update results with extracted metadata
    for idx, extracted in zip(pending_indices, extracted_results):
        result = results[idx]
        dataset_name = result['dataset_name']
        slug = result['slug']
        dataset_dir = Path(result['dataset_dir'])
        
        metadata_validated_path = dataset_dir / "metadata_validated.json"
        
        if extracted is not None and extracted.get('validation_passed', False):
            # Save validated metadata
            atomic_json(metadata_validated_path, extracted, indent=2)
            
            # Check publicness
            _resolve_public(extracted, Path(dataset_dir))
            if not extracted.get('is_public', False):
                if _is_insufficient_content(extracted):
                    result['status'] = 'error'
                    result['reason'] = 'insufficient_content'
                    result['error'] = 'Insufficient content to determine publicness (empty/placeholder metadata)'
                    continue
                result['status'] = 'rejected'
                result['reason'] = 'not_public_extraction'
                continue
            
            # Get download link
            download_info = _link_lookup(result['merged_results'], dataset_name,
                                         dataset_dir / "download_link.json", prompts_dir, use_gemini)
            
            # Write merged metadata
            merged_meta_path = dataset_dir / "metadata_merged.json"
            merged_meta = dict(extracted)
            merged_meta["dataset_name"] = extracted.get("dataset_name", dataset_name)
            merged_meta["slug"] = slug
            merged_meta["code_url"] = merged_meta.get("code_url") or download_info.get("code_url", "")
            merged_meta["download_url"] = merged_meta.get("download_url") or download_info.get("download_url", "")
            merged_meta["home_page_url"] = merged_meta.get("home_page_url") or download_info.get("home_page_url", "")
            merged_meta["download_status"] = download_info.get("status", "")
            merged_meta["is_pathology_dataset"] = True
            
            atomic_json(merged_meta_path, merged_meta, indent=2)
            
            # Update result
            result['status'] = 'completed'
            result['is_public'] = extracted.get('is_public', False)
            result['validation_passed'] = extracted.get('validation_passed', False)
            result['code_url'] = download_info.get('code_url', '')
            result['download_url'] = download_info.get('download_url', '')
            result['home_page_url'] = download_info.get('home_page_url', '')
        else:
            # Extraction failed
            result['status'] = 'error'
            result['error'] = 'Batch extraction failed after retries'
            
            # Save the failure for debugging -- not as metadata_validated.json, which later
            # runs would read as this dataset's extraction and never retry.
            failed_result = extracted or {
                'validation_passed': False,
                'validation_error': 'Batch extraction returned None'
            }
            atomic_json(dataset_dir / "metadata_failed.json", failed_result, indent=2)
        
        # Clean up merged_results from result (no longer needed)
        result.pop('merged_results', None)
    
    logger.info(f"Batch extraction completed for {len(pending_items)} datasets")
    return results


def run_individual_metadata_extraction(
    results: List[Dict],
    base_dir: Path,
    prompts_dir: Path,
    schema_path: Path,
    use_gemini: bool = False
) -> List[Dict]:
    """
    Run individual metadata extraction for datasets (non-batch mode).
    
    Args:
        results: List of dataset processing results
        base_dir: Base directory for dataset data
        prompts_dir: Directory containing prompts
        schema_path: Path to dataset schema
        use_gemini: Whether to use Gemini
        
    Returns:
        Updated list of results with extraction completed
    """
    from .dataset_validator import extract_dataset_metadata, normalize_metadata, find_download_link
    
    ex_data_prompt = prompts_dir / "ex_data.md"
    
    # One dataset at a time this was two flex calls (~1 min) a dataset: ~18 h for a full
    # run's ~1,100. Each dataset's result is its own; WSI_LLM_THREADS threads (default 48).
    def _extract_one(result):
        if result.get('status') != 'pending_extraction':
            return
        
        dataset_name = result['dataset_name']
        slug = result['slug']
        dataset_dir = Path(result['dataset_dir'])
        merged_results = result.get('merged_results')
        
        if not merged_results:
            result['status'] = 'error'
            result['error'] = 'No merged results available'
            return
        
        metadata_raw_path = dataset_dir / "metadata_raw.json"
        metadata_validated_path = dataset_dir / "metadata_validated.json"
        
        # Retry logic (3 attempts)
        validation_passed = False
        last_validation = {}
        
        for attempt in range(1, 4):
            logger.info(f"Metadata extraction attempt {attempt}/3 for {dataset_name}")
            metadata_raw = extract_dataset_metadata(
                merged_results, dataset_name, ex_data_prompt, schema_path, use_gemini, entity_type='dataset'
            )
            
            # Persist raw each attempt
            atomic_json(metadata_raw_path, metadata_raw, indent=2)
            if metadata_raw.get('error'):  # the call failed: nothing to validate
                last_validation = {'error': metadata_raw['error']}
                continue
            
            metadata_validated = normalize_metadata(metadata_raw, schema_path)
            atomic_json(metadata_validated_path, metadata_validated, indent=2)
            
            validation_passed = metadata_validated.get('validation_passed', False)
            last_validation = metadata_validated
            if validation_passed:
                break
            logger.warning(f"Validation failed on attempt {attempt} for {dataset_name}: {metadata_validated.get('validation_error', 'unknown error')}")
        
        metadata_validated = last_validation
        if _extraction_call_failed(metadata_validated):
            # Every call failed: leave nothing cached, so the next run extracts again.
            metadata_validated_path.unlink(missing_ok=True)
            result['status'] = 'error'
            result['reason'] = 'extraction_call_failed'
            result['error'] = metadata_validated.get('error', '')
            result.pop('merged_results', None)
            return
        
        # Check publicness
        _resolve_public(metadata_validated, Path(dataset_dir))
        if not metadata_validated.get('is_public', False):
            if _is_insufficient_content(metadata_validated):
                result['status'] = 'error'
                result['reason'] = 'insufficient_content'
                result['error'] = 'Insufficient content to determine publicness (empty/placeholder metadata)'
                result.pop('merged_results', None)
                return
            result['status'] = 'rejected'
            result['reason'] = 'not_public_extraction'
            result.pop('merged_results', None)
            return
        
        if not validation_passed:
            result['status'] = 'error'
            result['error'] = f"Validation failed after 3 attempts: {metadata_validated.get('validation_error', 'unknown')}"
            result.pop('merged_results', None)
            return
        
        # Get download link
        download_info = _link_lookup(merged_results, dataset_name, dataset_dir / "download_link.json",
                                     prompts_dir, use_gemini)
        
        # Write merged metadata
        merged_meta_path = dataset_dir / "metadata_merged.json"
        merged_meta = dict(metadata_validated)
        merged_meta["dataset_name"] = metadata_validated.get("dataset_name", dataset_name)
        merged_meta["slug"] = slug
        merged_meta["code_url"] = merged_meta.get("code_url") or download_info.get("code_url", "")
        merged_meta["download_url"] = merged_meta.get("download_url") or download_info.get("download_url", "")
        merged_meta["home_page_url"] = merged_meta.get("home_page_url") or download_info.get("home_page_url", "")
        merged_meta["download_status"] = download_info.get("status", "")
        merged_meta["is_pathology_dataset"] = True
        
        atomic_json(merged_meta_path, merged_meta, indent=2)
        
        # Update result
        result['status'] = 'completed'
        result['is_public'] = metadata_validated.get('is_public', False)
        result['validation_passed'] = metadata_validated.get('validation_passed', False)
        result['code_url'] = download_info.get('code_url', '')
        result['download_url'] = download_info.get('download_url', '')
        result['home_page_url'] = download_info.get('home_page_url', '')
        
        # Clean up merged_results
        result.pop('merged_results', None)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, int(os.getenv("WSI_LLM_THREADS", "48")))) as ex:
        list(ex.map(_extract_one, results))

    return results


def run_dataset_search_pipeline(
    dataset_names: List[str],
    base_dir: Path,
    prompts_dir: Path,
    schema_path: Path,
    use_gemini: bool = False,
    use_perplexity: bool = False,
    max_pages: int = 10,
    num_workers: Optional[int] = None,
    use_browser: bool = False,
    use_batch: bool = False,
    descriptions_by_name: Optional[Dict[str, List[str]]] = None,
) -> List[Dict]:
    """
    Run the dataset search pipeline with multiprocessing.
    
    Args:
        dataset_names: List of dataset names to process
        base_dir: Base directory for dataset data
        prompts_dir: Directory containing prompts
        schema_path: Path to dataset schema
        use_gemini: Whether to use Gemini
        use_perplexity: Whether to use Perplexity search
        max_pages: Max pages to fetch per dataset
        num_workers: Number of worker processes (default: WSI_DATASET_PROCS, 24)
        use_browser: Whether to use browser fallback for failed fetches
        use_batch: Whether to use batch API for LLM calls with validation retries
        
    Returns:
        List of result dicts
    """
    if num_workers is None:
        # The work is web search, page fetches and LLM calls, not CPU: the old
        # cpu_count() - 1 (9 here) left 2.2 at ~46 s a dataset. 24 processes (~6 GB)
        # keep each site under its 2 s pacing (polite_get) while fetching 2-3x as fast.
        num_workers = max(1, int(os.getenv("WSI_DATASET_PROCS", "24")))
    
    # Check for existing results file
    results_path = base_dir / "dataset_processing_results.json"
    existing_results = {}
    
    if results_path.exists():
        logger.info(f"Loading existing results from {results_path}")
        try:
            with open(results_path, 'r', encoding="utf-8") as f:
                data = json.load(f)
                for r in data.get('results', []):
                    existing_results[r['dataset_name']] = r
            logger.info(f"Loaded {len(existing_results)} existing dataset results")
        except Exception as e:
            logger.warning(f"Failed to load existing results: {e}")
    
    # Filter out already completed datasets
    datasets_to_process = []
    for name in dataset_names:
        if name in existing_results:
            status = existing_results[name].get('status')
            # A "rejection" recorded from a failed validation call is not a verdict.
            if status == 'completed' or (status == 'rejected'
                                         and existing_results[name].get('reason') != 'validation_error'):
                logger.info(f"Skipping {name} (already {status})")
                continue
        datasets_to_process.append(name)
    
    if not datasets_to_process:
        logger.info("All datasets already processed, loading existing results")
        return list(existing_results.values())
    
    logger.info(f"Processing {len(datasets_to_process)} datasets ({len(existing_results)} already done)")
    logger.info(f"Using {num_workers} worker processes")
    
    # Prepare config for each dataset
    config = {
        'base_dir': str(base_dir),
        'prompts_dir': str(prompts_dir),
        'schema_path': str(schema_path),
        'use_gemini': use_gemini,
        'use_perplexity': use_perplexity,
        'max_pages': max_pages,
        'use_browser': use_browser,
        'use_batch': use_batch,
        # How the papers describe each dataset: anchors 2.2 on the release they mean.
        'descriptions': {n: list((descriptions_by_name or {}).get(n) or []) for n in datasets_to_process},
    }
    
    # Create args for each dataset
    args_list = [(name, config) for name in datasets_to_process]
    
    # Process with multiprocessing (search, fetch, validate)
    new_results = []
    
    if num_workers == 1:
        # Sequential processing for debugging
        for args in tqdm(args_list, desc="Processing datasets (search & fetch)"):
            result = process_single_dataset(args)
            new_results.append(result)
    else:
        # Parallel processing
        with Pool(processes=pool_size(num_workers)) as pool:
            for result in tqdm(pool.imap(_process_in_worker, args_list), total=len(args_list),
                               desc="Processing datasets (search & fetch)"):
                if NOT_SAVED in result:  # leaving the block terminates the pool
                    raise ResponseNotSaved(result[NOT_SAVED])
                new_results.append(result)
    
    # Metadata extraction for all datasets that need it
    if use_batch:
        logger.info("Running batch metadata extraction for all datasets...")
        new_results = run_batch_metadata_extraction(
            results=new_results,
            base_dir=base_dir,
            prompts_dir=prompts_dir,
            schema_path=schema_path,
            use_gemini=use_gemini
        )
    else:
        # Non-batch mode: process each dataset individually with retries
        logger.info("Running individual metadata extraction for pending datasets...")
        new_results = run_individual_metadata_extraction(
            results=new_results,
            base_dir=base_dir,
            prompts_dir=prompts_dir,
            schema_path=schema_path,
            use_gemini=use_gemini
        )
    
    # Merge with existing results
    all_results = list(existing_results.values())
    for r in new_results:
        # Update or add
        existing_results[r['dataset_name']] = r
    all_results = list(existing_results.values())
    
    # Save updated results
    output_data = {
        'total_datasets': len(all_results),
        'results': all_results
    }
    atomic_json(results_path, output_data, indent=2)
    logger.info(f"Saved results to {results_path}")
    
    logger.info(f"Completed dataset search pipeline")
    
    # Summary
    completed = sum(1 for r in all_results if r['status'] == 'completed')
    rejected = sum(1 for r in all_results if r['status'] == 'rejected')
    errors = sum(1 for r in all_results if r['status'] == 'error')
    
    logger.info(f"Results: {completed} completed, {rejected} rejected, {errors} errors")
    if errors:
        from collections import Counter
        why = Counter(r.get('reason') or 'other' for r in all_results if r['status'] == 'error')
        logger.warning(f"{errors} datasets errored out of this run and are retried on the next one "
                       f"(they are not in the catalog until then): {dict(why)}")
    
    return all_results


def build_datasets_seed_json(
    results: List[Dict],
    base_dir: Path,
    output_path: Path,
    parents_by_name: Optional[Dict[str, str]] = None,
) -> List[Dict]:
    """
    Build datasets_seed.json from processing results.
    
    Args:
        results: List of processing results
        base_dir: Base directory for dataset data
        output_path: Path to save datasets_seed.json
        
    Returns:
        List of public dataset dicts
    """
    public_datasets = []
    
    for result in results:
        if result.get('status') != 'completed':
            continue

        # Load merged metadata (preferred) else validated metadata
        dataset_dir = Path(result['dataset_dir'])
        merged_path = dataset_dir / "metadata_merged.json"
        metadata_path = dataset_dir / "metadata_validated.json"

        metadata = None
        if merged_path.exists():
            with open(merged_path, 'r', encoding="utf-8") as f:
                metadata = json.load(f)
        elif metadata_path.exists():
            with open(metadata_path, 'r', encoding="utf-8") as f:
                metadata = json.load(f)

        if not metadata:
            continue

        # Only include validated and public datasets
        if not metadata.get('validation_passed', False):
            continue
        if not metadata.get('is_public', False):
            continue

        # Ensure URL fields are included (may be empty if not found)
        if 'code_url' not in metadata or 'download_url' not in metadata or 'home_page_url' not in metadata:
            # Try to read from download_link.json
            dl_path = dataset_dir / "download_link.json"
            if dl_path.exists():
                try:
                    dl = json.load(open(dl_path, 'r', encoding="utf-8"))
                    metadata['code_url'] = dl.get('code_url', '')
                    metadata['download_url'] = dl.get('download_url', '')
                    metadata['home_page_url'] = dl.get('home_page_url', '')
                except Exception:
                    metadata['code_url'] = ''
                    metadata['download_url'] = ''
                    metadata['home_page_url'] = ''

        # Subset relation from the canonical catalog (TCGA-KICH -> TCGA-RCC), as the
        # slug the parent gets in this same file.
        parent = (parents_by_name or {}).get(result['dataset_name'])
        metadata['parent_slug'] = (parent.lower().replace(' ', '-').replace('_', '-') if parent else '')

        public_datasets.append(metadata)
    
    # Save to output
    output_data = {"datasets": public_datasets}
    atomic_json(output_path, output_data, indent=2)
    
    logger.info(f"Built datasets_seed.json with {len(public_datasets)} public datasets")
    return public_datasets
