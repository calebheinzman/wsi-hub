"""
Metrics extraction module for extracting performance metrics from WSI papers.

This module classifies papers by task type (classification, segmentation, detection, 
regression, survival) and then extracts relevant metrics for each task type.
"""

import base64
import json
import re
import os
import time
import random
import threading
from pathlib import Path
from typing import Dict, List, Optional, Any

from pipeline.utils.llms.llm import (
    run_gpt, 
    validate_json_response, 
    is_all_empty_values,
    _extract_batch_content
)
from pipeline.utils.llms.batch_manager import BatchProcessor
from pipeline.src.checkpoint import fingerprint, is_permanent_error, run_items, thread_runner
from pipeline.src.paper_code.dedupe import dedupe_key
from pipeline.src.paper_code.filters import open_access_pdf, tables_missing, text_source
from ..utils import get_logger, read_json, write_json

logger = get_logger(__name__)


def _batch_error_summary(result: Optional[Dict[str, Any]]) -> str:
    if not result:
        return "no_result"
    try:
        if result.get('error'):
            return f"error={result.get('error')}"
        resp = result.get('response') or {}
        status_code = resp.get('status_code')
        body = resp.get('body') or {}
        body_error = body.get('error')
        if status_code or body_error:
            return f"status_code={status_code}, body_error={body_error}"
    except Exception:
        return "error_summary_failed"
    return "unknown"

# Default classification result for failures
DEFAULT_TASK_CLASSIFICATION = {
    "classification": False,
    "segmentation": False,
    "detection": False,
    "regression": False,
    "survival": False
}

# Calls that failed for good. A failed 1.7 call leaves all five flags false and a failed
# 1.8 call leaves no rows, which look like "no tasks" and "no results"; this list keeps
# them apart. The CLI writes it to paper_data/failed_calls.json.
FAILED_CALLS: List[Dict] = []


def _threads() -> int:
    """WSI_LLM_THREADS (default 48): 1.7 and 1.8 calls wait on the API; one at a time, 1.7
    took ~9 s a paper on flex (13 h for a full run). Threads cost little memory, and 48
    use ~5M of the 180M tokens a minute allowed (1.8 calls with PDFs)."""
    return max(1, int(os.getenv("WSI_LLM_THREADS", "48")))


def _failed(step: str, paper: Dict, error: Any, task_type: str = "") -> None:
    FAILED_CALLS.append({"step": step, "paper_id": paper.get('link') or paper.get('title', ''),
                         "task_type": task_type, "error": str(error or "no valid response")[:300]})


SPLIT_PRIORITY = ["test", "external_test", "cross_validation", "validation", "other", "train"]
_SPLIT_SYNONYMS = {
    "test": "test", "testing": "test", "internal_test": "test", "held_out_test": "test", "holdout": "test",
    "hold_out": "test", "external_test": "external_test", "external": "external_test",
    "external_validation": "external_test", "independent_test": "external_test",
    "cross_validation": "cross_validation", "cv": "cross_validation", "k_fold": "cross_validation",
    "validation": "validation", "val": "validation", "internal_validation": "validation", "dev": "validation",
    "train": "train", "training": "train", "other": "other",
}


def _norm_split(value: Any) -> str:
    key = re.sub(r"[^a-z]+", "_", str(value or "").lower()).strip("_")
    if not key:
        return ""
    if key in _SPLIT_SYNONYMS:
        return _SPLIT_SYNONYMS[key]
    if "external" in key:
        return "external_test"
    if "cross" in key or "fold" in key:
        return "cross_validation"
    if "valid" in key:
        return "validation"
    if "test" in key:
        return "test"
    return "other"


def value_in_text(value: Any, text: str) -> bool:
    """Whether a metric value is written in the text: 0.953 as 0.953, .953 or 95.3,
    0.6 as 0.60 -- only spellings of exactly this number, never a rounding. Copes with
    PDF text that splits numbers ("0 .853") and with decimal commas."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    exact = lambda x: {f"{x:.{d}f}" for d in range(5) if abs(round(x, d) - x) < 1e-9}  # noqa: E731
    forms = exact(v)
    if 0 < v <= 1:
        forms |= exact(round(v * 100, 6))
    forms |= {f[1:] for f in forms if f.startswith("0.")}
    forms = {f for f in forms if len(f.strip(".")) >= 2}
    text = re.sub(r"(\d),(\d)", r"\1.\2", re.sub(r"(\d)\s+\.\s*(\d)", r"\1.\2", text or ""))
    return any(re.search(rf"(?<![\d.]){re.escape(f)}(?!\d)", text) for f in forms)


def paper_abstract(paper: Dict) -> str:
    """The paper's abstract: the summary step's, else the text between an "Abstract"
    heading and the introduction, else the opening of the text."""
    sp = (paper.get("extraction") or {}).get("sum_paper.md")
    if isinstance(sp, dict) and isinstance(sp.get("Abstract"), str) and sp["Abstract"].strip():
        return sp["Abstract"]
    text = paper.get("fulltext") or ""
    m = re.search(r"\babstract\b", text[:6000], re.I)
    start = m.end() if m else 0
    end = re.search(r"\b(?:1\.?\s*)?introduction\b", text[start:start + 6000], re.I)
    return text[start:start + (end.start() if end else 2500)]


PDF_NOTE = ("The paper's PDF is attached. Its tables, which the text below lacks (they are images on the "
            "page), hold the results: read the results from the PDF.\n\n")


def _table_pdfs(papers: List[Dict]) -> Dict[int, bytes]:
    """paper index -> its open-access PDF, for papers whose text names tables it lacks.

    Publisher pages (and some PDFs: IEEE Access) render results tables as images, so the
    text a model reads has the captions and none of the numbers. The PDF goes with each
    metrics call so the model reads the tables. WSI_PDF_ATTACH=0 disables.
    """
    if os.getenv("WSI_PDF_ATTACH", "1").lower() in ("0", "false", "no"):
        return {}
    from concurrent.futures import ThreadPoolExecutor
    want = [i for i, p in enumerate(papers) if tables_missing(p.get("fulltext") or "")]
    if not want:
        return {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        found = dict(zip(want, ex.map(lambda i: open_access_pdf(papers[i]), want)))
    out = {i: pdf for i, pdf in found.items() if pdf}
    logger.info(f"Metrics: {len(want)} papers' text lacks its tables; open-access PDF attached for {len(out)}")
    return out


def _pdf_wanted(papers: List[Dict]) -> set:
    """Indexes of the papers whose text names tables it lacks (WSI_PDF_ATTACH=0: none)."""
    if os.getenv("WSI_PDF_ATTACH", "1").lower() in ("0", "false", "no"):
        return set()
    return {i for i, p in enumerate(papers) if tables_missing(p.get("fulltext") or "")}


def _with_pdf(pdfs: Dict[int, bytes], idx: int, text: str) -> Dict:
    """run_gpt arguments for a paper's metrics call: its text, plus the PDF if it has one."""
    if idx in pdfs:
        return {"user_prompt": PDF_NOTE + text, "attachments": [("paper.pdf", pdfs[idx])]}
    return {"user_prompt": text}


def _batch_with_pdf(pdfs: Dict[int, bytes], idx: int, text: str) -> Dict:
    """_with_pdf for a batch request: the PDF as base64, so the request serializes."""
    args = _with_pdf(pdfs, idx, text)
    if "attachments" in args:
        args["attachments"] = [(name, base64.b64encode(data).decode()) for name, data in args["attachments"]]
    return args


def _fix_primary(records: List[Dict], abstract: str = "") -> None:
    """Exactly one primary result per dataset.

    Among a dataset's overall results (subset ""), the headline is the one the abstract
    quotes: a paper's abstract states its headline numbers. The model's own pick stands
    when the abstract quotes it, or quotes none of them; with no pick, the best split
    wins (test before validation). A dataset reported only per class or subgroup gets
    the abstract-quoted entry as its headline, when there is one.
    """
    by_ds: Dict[str, List[Dict]] = {}
    for r in records:
        by_ds.setdefault(r["dataset"].lower(), []).append(r)
    rank = lambda r: SPLIT_PRIORITY.index(r["split"]) if r["split"] in SPLIT_PRIORITY else 4  # noqa: E731
    quoted = lambda r: bool(abstract) and any(  # noqa: E731
        value_in_text(v, abstract) for v in r["metrics"].values() if v not in (None, ""))
    for entries in by_ds.values():
        overall = [r for r in entries if not r["subset"]]
        group = overall or [r for r in entries if quoted(r)][:1]
        for r in entries:
            if r not in group:
                r["is_primary"] = False
        if not group:
            continue
        picked = [r for r in group if r["is_primary"]]
        # The headline is the paper's own method: the one the model picked. An abstract
        # that also quotes a comparison row must not make that row the headline.
        own = {(r.get("method") or "").lower() for r in picked}
        ours = lambda r: not own or (r.get("method") or "").lower() in own  # noqa: E731
        in_abstract = [r for r in group if quoted(r) and ours(r)]
        if in_abstract and not any(r in in_abstract for r in picked):
            picked = in_abstract
        best = picked[0] if len(picked) == 1 else min(picked or group, key=rank)
        for r in group:
            r["is_primary"] = r is best


def _records_from_response(paper: Dict, idx: int, task_type: str, parsed: Any) -> List[Dict]:
    """Metric records for one task from one response: one per (dataset, split, level,
    subset) entry. A flat legacy dict of metrics becomes a single primary record."""
    if isinstance(parsed, dict) and isinstance(parsed.get("results"), list):
        entries = parsed["results"]
    elif isinstance(parsed, list):
        entries = parsed
    elif isinstance(parsed, dict):
        entries = [{"metrics": parsed, "is_primary": True}]
    else:
        return []
    records = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        m = e.get("metrics")
        if not isinstance(m, dict) or is_all_empty_values(m):
            continue
        rec = _record_from_metrics(paper, idx, task_type, m)
        rec.update({
            "dataset": " ".join(str(e.get("dataset") or "").split()),
            "split": _norm_split(e.get("split")),
            "level": str(e.get("level") or "").strip().lower(),
            "subset": " ".join(str(e.get("subset") or "").split()),
            "is_primary": e.get("is_primary") is True,
            # Where the row comes from, so a published number can be checked against the paper.
            **{k: " ".join(str(e.get(k) or "").split()) for k in ("method", "n", "uncertainty", "evidence")},
        })
        records.append(rec)
    _fix_primary(records, paper_abstract(paper))
    return records


def classify_paper_tasks(
    papers: List[Dict],
    prompt_path: Path,
    schema_path: Path,
    use_gemini: bool = False,
    use_batch: bool = False,
    output_path: Optional[Path] = None
) -> List[Dict]:
    """
    Classify papers by task type (classification, segmentation, detection, regression, survival).
    
    Args:
        papers: List of paper records with 'fulltext' field
        prompt_path: Path to task classification prompt
        schema_path: Path to task classification schema
        use_gemini: If True, use Gemini API instead of OpenAI
        use_batch: If True, use batch API for LLM calls
        output_path: Optional path to save results
    
    Returns:
        List of papers with 'task_classification' field added
    """
    prompt_template = prompt_path.read_text(encoding="utf-8")
    schema = read_json(schema_path)
    
    # Model selection
    if use_gemini:
        model_name = os.getenv('WSI_EXTRACTION_MODEL', 'gemini-2.5-pro')
    else:
        model_name = os.getenv('WSI_EXTRACTION_MODEL', 'gpt-6-luna')
    
    results = [paper.copy() for paper in papers]

    # Filter papers with fulltext. Papers that already carry a non-empty
    # task_classification are passed through untouched -- no LLM call.
    papers_with_text = [
        (i, paper) for i, paper in enumerate(papers)
        if paper.get('fulltext') and not paper.get('task_classification')
    ]

    if not papers_with_text:
        logger.info("Task classification: nothing to do (all papers already classified or no fulltext)")
        return results
    
    if use_batch:
        logger.info(f"Classifying {len(papers_with_text)} papers by task type using batch API...")
        processor = BatchProcessor(provider="gemini" if use_gemini else "gpt")
        
        # Build batch requests
        requests = []
        for idx, paper in papers_with_text:
            requests.append({
                'custom_id': f"task_class_{idx}",
                'system_prompt': prompt_template,
                'user_prompt': paper['fulltext'],
                'model': model_name,
                'response_format': {'type': 'json_object'},
                'structured_output_schema': schema
            })
        
        # Submit batch
        batch_results = processor.get_or_create_batch(
            requests=requests,
            step_name="task_classification",
            model=model_name
        )
        
        # Parse and validate results, tracking failures for retry
        failed_items: List[tuple] = []  # (idx, paper)
        
        for idx, paper in papers_with_text:
            custom_id = f"task_class_{idx}"
            result = batch_results.get(custom_id)
            content = _extract_batch_content(result, use_gemini)
            
            if content:
                is_valid, parsed, error = validate_json_response(
                    content,
                    reject_all_empty=True
                )
                if is_valid and parsed:
                    results[idx]['task_classification'] = parsed
                else:
                    logger.warning(f"Invalid JSON for task classification {custom_id}: {error}")
                    failed_items.append((idx, paper))
                    results[idx]['task_classification'] = DEFAULT_TASK_CLASSIFICATION.copy()
            else:
                logger.warning(f"No content for task classification {custom_id}")
                failed_items.append((idx, paper))
                results[idx]['task_classification'] = DEFAULT_TASK_CLASSIFICATION.copy()
        
        # Retry failed items
        max_retries = int(os.getenv('WSI_BATCH_RETRIES', '2'))
        for retry_attempt in range(max_retries):
            if not failed_items:
                break
            
            logger.info(f"Retry attempt {retry_attempt + 1}/{max_retries} for {len(failed_items)} failed task classification items")
            
            retry_requests = []
            for idx, paper in failed_items:
                retry_requests.append({
                    'custom_id': f"task_class_{idx}",
                    'system_prompt': prompt_template,
                    'user_prompt': paper['fulltext'],
                    'model': model_name,
                    'response_format': {'type': 'json_object'},
                })
            
            retry_results = processor.get_or_create_batch(
                requests=retry_requests,
                step_name=f"task_classification_retry{retry_attempt + 1}",
                model=model_name
            )
            
            next_failed = []
            for idx, paper in failed_items:
                custom_id = f"task_class_{idx}"
                result = retry_results.get(custom_id)
                content = _extract_batch_content(result, use_gemini)
                
                if content:
                    is_valid, parsed, error = validate_json_response(content, reject_all_empty=True)
                    if is_valid and parsed:
                        results[idx]['task_classification'] = parsed
                        continue
                
                next_failed.append((idx, paper))
            
            failed_items = next_failed
        
        if failed_items:
            logger.warning(f"Task classification completed with {len(failed_items)} items still failing")
            for idx, paper in failed_items:
                results[idx]['task_classification_error'] = "no valid response after batch retries"
                _failed("1.7_task_classification", paper, "no valid response after batch retries")
    
    else:
        logger.info(f"Classifying {len(papers_with_text)} papers by task type...")
        def _classify_one(work):
            idx, paper = work
            max_attempts = int(os.getenv('WSI_LLM_RETRIES', '3'))
            last_error = None
            raised = False
            
            for attempt in range(max_attempts):
                try:
                    response = run_gpt(
                        system_prompt=prompt_template,
                        user_prompt=paper['fulltext'],
                        model=model_name,
                        item=paper.get('link') or paper.get('title') or "",
                        response_format={'type': 'json_object'},
                        use_gemini=use_gemini,
                        validate_json=True,
                        reject_all_empty=True,
                        max_retries=1
                    )
                    content = (response.choices[0].message.content or "{}").strip()
                    raised = False
                    
                    is_valid, parsed, error = validate_json_response(content, reject_all_empty=True)
                    if is_valid and parsed:
                        return {"task_classification": parsed}
                    else:
                        last_error = error
                        logger.warning(f"Invalid task classification for paper {idx} (attempt {attempt + 1}): {error}")
                        
                except Exception as e:
                    raised = not is_permanent_error(e)  # a 400 (e.g. too long) won't change
                    last_error = str(e)
                    logger.warning(f"Error classifying paper {idx} (attempt {attempt + 1}): {e}")
                
                if attempt < max_attempts - 1:
                    time.sleep(min(2.0 * (2 ** attempt), 10.0) + random.random() * 0.25)
            logger.warning(f"Failed to classify paper {idx} after {max_attempts} attempts: {last_error}")
            return {"error": str(last_error or "no valid response")[:300], "raised": raised}

        # Checkpointed per paper; a call that raised (API / network) is retried, an invalid
        # answer is final.
        done, failing = run_items(
            "1.7_task_classification", papers_with_text, key=lambda w: dedupe_key(w[1]), fn=_classify_one,
            runner=thread_runner(_threads(), "Task classification"),
            failed=lambda res: bool(res.get("raised")), fp=fingerprint(prompt_template, model_name))
        last = {dedupe_key(w[1]): res for w, res in failing}
        for idx, paper in papers_with_text:
            res = done.get(dedupe_key(paper)) or last.get(dedupe_key(paper))
            res = res if isinstance(res, dict) else {"error": str(getattr(res, "error", "no valid response"))}
            if "task_classification" in res:
                results[idx]['task_classification'] = res["task_classification"]
            else:
                results[idx]['task_classification'] = DEFAULT_TASK_CLASSIFICATION.copy()
                results[idx]['task_classification_error'] = res["error"]
                _failed("1.7_task_classification", paper, res["error"])
    
    if output_path:
        write_json(output_path, results)
        logger.info(f"Task classification results saved to {output_path}")
    
    return results


def _record_from_metrics(paper: Dict, idx: int, task_type: str, metrics: Dict) -> Dict:
    paper_id = paper.get('link') or paper.get('url') or paper.get('title', f"paper_{idx}")
    return {
        'paper_id': paper_id,
        'paper_title': paper.get('title', ''),
        'paper_year': paper.get('year'),
        'task_type': task_type,
        'metrics': metrics,
        # full_text, or abstract when only the abstract page was available: results
        # from an abstract are the headline numbers at best.
        'text_source': text_source(paper.get('fulltext', '')),
    }


def extract_metrics_for_papers(
    papers: List[Dict],
    metrics_config_path: Path,
    metrics_prompt_path: Path,
    schemas_dir: Path,
    use_gemini: bool = False,
    use_batch: bool = False,
    output_path: Optional[Path] = None
) -> List[Dict]:
    """
    Extract metrics from papers based on their task classifications.
    
    Args:
        papers: List of papers with 'task_classification' field
        metrics_config_path: Path to metrics_config.json
        metrics_prompt_path: Path to metrics extraction prompt template
        schemas_dir: Directory containing task-specific metric schemas
        use_gemini: If True, use Gemini API
        use_batch: If True, use batch API
        output_path: Optional path to save results
    
    Returns:
        List of metric records with paper_id, task_type, and metrics
    """
    metrics_config = read_json(metrics_config_path)
    prompt_template = metrics_prompt_path.read_text(encoding="utf-8")

    # Model selection
    if use_gemini:
        model_name = os.getenv('WSI_EXTRACTION_MODEL', 'gemini-2.5-pro')
    else:
        model_name = os.getenv('WSI_EXTRACTION_MODEL', 'gpt-6-luna')

    # Prepare extraction tasks: (paper_idx, task_type, paper)
    extraction_tasks = []
    for idx, paper in enumerate(papers):
        task_classification = paper.get('task_classification', {})
        if not task_classification:
            continue
        
        # For each active task type, create an extraction task
        for task_type in ['classification', 'segmentation', 'detection', 'regression', 'survival']:
            if task_classification.get(task_type, False):
                extraction_tasks.append((idx, task_type, paper))
    
    if not extraction_tasks:
        logger.warning("No papers with active tasks found for metrics extraction")
        return []
    
    logger.info(f"Extracting metrics for {len(extraction_tasks)} task-paper combinations")
    
    results = []
    
    # Load schemas
    schemas = {}
    for task_type in ['classification', 'segmentation', 'detection', 'regression', 'survival']:
        schema_path = schemas_dir / f"{task_type}_schema.json"
        if schema_path.exists():
            schemas[task_type] = read_json(schema_path)
    
    # Helper to build prompt for a task
    def _build_prompt(task_type: str) -> str:
        task_config = metrics_config.get(task_type, {})
        prompt_base = task_config.get('prompt_base', '')
        json_template = json.dumps(task_config.get('json_template', {}), indent=2)
        return prompt_template.format(
            prompt_base=prompt_base,
            dataset_info="Paper content (all datasets mentioned in paper)",
            json_template=json_template
        )
    
    if use_batch:
        logger.info(f"Using batch API for metrics extraction...")
        processor = BatchProcessor(provider="gemini" if use_gemini else "gpt")
        
        # Build batch requests
        requests = []
        pdfs = _table_pdfs(papers)
        for idx, task_type, paper in extraction_tasks:
            full_prompt = _build_prompt(task_type)
            requests.append({
                'custom_id': f"metrics_{idx}_{task_type}",
                'system_prompt': full_prompt,
                **_batch_with_pdf(pdfs, idx, paper.get('fulltext', '')),
                'model': model_name,
                'response_format': {'type': 'json_object'},
                'structured_output_schema': schemas.get(task_type)
            })
        
        # Submit batch
        batch_results = processor.get_or_create_batch(
            requests=requests,
            step_name="metrics_extraction",
            model=model_name
        )
        
        # Parse and validate results, tracking failures for retry
        failed_items: List[tuple] = []  # (idx, task_type, paper, prompt)
        
        for idx, task_type, paper in extraction_tasks:
            custom_id = f"metrics_{idx}_{task_type}"
            result = batch_results.get(custom_id)
            content = _extract_batch_content(result, use_gemini)
            
            
            if content:
                is_valid, parsed, error = validate_json_response(content, reject_all_empty=False)
                if is_valid and parsed:
                    if is_all_empty_values(parsed):
                        continue
                    results.extend(_records_from_response(paper, idx, task_type, parsed))
                else:
                    logger.warning(f"Invalid JSON for metrics {custom_id}: {error}")
                    failed_items.append((idx, task_type, paper, _build_prompt(task_type)))
            else:
                logger.warning(f"No content for metrics {custom_id} ({_batch_error_summary(result)})")
                failed_items.append((idx, task_type, paper, _build_prompt(task_type)))
        
        # Retry failed items
        max_retries = int(os.getenv('WSI_BATCH_RETRIES', '2'))
        for retry_attempt in range(max_retries):
            if not failed_items:
                break
            
            logger.info(f"Retry attempt {retry_attempt + 1}/{max_retries} for {len(failed_items)} failed metrics items")
            
            retry_requests = []
            for idx, task_type, paper, prompt in failed_items:
                retry_requests.append({
                    'custom_id': f"metrics_{idx}_{task_type}",
                    'system_prompt': prompt,
                    **_batch_with_pdf(pdfs, idx, paper.get('fulltext', '')),
                    'model': model_name,
                    'response_format': {'type': 'json_object'},
                })
            
            retry_results = processor.get_or_create_batch(
                requests=retry_requests,
                step_name=f"metrics_extraction_retry{retry_attempt + 1}",
                model=model_name
            )
            
            next_failed = []
            retry_missing_content = 0
            retry_invalid_json = 0
            for idx, task_type, paper, prompt in failed_items:
                custom_id = f"metrics_{idx}_{task_type}"
                result = retry_results.get(custom_id)
                content = _extract_batch_content(result, use_gemini)
                
                
                if content:
                    is_valid, parsed, error = validate_json_response(content, reject_all_empty=False)
                    if is_valid and parsed:
                        if is_all_empty_values(parsed):
                            continue
                        results.extend(_records_from_response(paper, idx, task_type, parsed))
                        continue
                    retry_invalid_json += 1
                else:
                    retry_missing_content += 1
                
                next_failed.append((idx, task_type, paper, prompt))

            if retry_missing_content or retry_invalid_json:
                logger.info(
                    "Retry %d/%d metrics batch: %d missing content, %d invalid JSON (%d total attempted)",
                    retry_attempt + 1,
                    max_retries,
                    retry_missing_content,
                    retry_invalid_json,
                    len(failed_items),
                )
            
            failed_items = next_failed
        
        if failed_items:
            logger.warning(f"Metrics extraction completed with {len(failed_items)} items still failing")
            for idx, task_type, paper, _prompt in failed_items:
                _failed("1.8_metrics", paper, "no valid response after batch retries", task_type)
    
    else:
        logger.info(f"Extracting metrics (non-batch mode)...")
        # The PDF is read when its call runs (open_access_pdf caches it on disk): holding
        # every needed PDF in memory first was gigabytes at full-run size.
        # One search per paper, not per task: a paper's tasks run side by side, and
        # repositories refuse repeated downloads. Found PDFs come back from the disk cache.
        pdf_wanted = _pdf_wanted(papers)
        pdf_locks = {i: threading.Lock() for i in pdf_wanted}
        pdf_found: Dict[int, bool] = {}

        def _pdf_for(idx: int, paper: Dict) -> bytes:
            if idx not in pdf_wanted:
                return b""
            with pdf_locks[idx]:
                if pdf_found.get(idx) is False:
                    return b""
                pdf = open_access_pdf(paper)
                pdf_found[idx] = bool(pdf)
                return pdf

        def _extract_one(work):
            idx, task_type, paper = work
            full_prompt = _build_prompt(task_type)
            pdf = _pdf_for(idx, paper)
            pdfs = {idx: pdf} if pdf else {}
            max_attempts = int(os.getenv('WSI_LLM_RETRIES', '3'))
            last_error = None
            raised = False
            
            for attempt in range(max_attempts):
                try:
                    response = run_gpt(
                        system_prompt=full_prompt,
                        **_with_pdf(pdfs, idx, paper.get('fulltext', '')),
                        model=model_name,
                        item=f"{paper.get('link') or paper.get('title') or ''} [{task_type}]",
                        response_format={'type': 'json_object'},
                        use_gemini=use_gemini,
                        validate_json=True,
                        reject_all_empty=False,
                        max_retries=1
                    )
                    
                    content = (response.choices[0].message.content or "{}").strip()
                    raised = False
                    is_valid, parsed, error = validate_json_response(content, reject_all_empty=False)
                    
                    if is_valid and parsed:
                        if is_all_empty_values(parsed):
                            return {"records": []}
                        return {"records": _records_from_response(paper, idx, task_type, parsed)}
                    else:
                        last_error = error
                        logger.warning(f"Invalid metrics for paper {idx}, task {task_type} (attempt {attempt + 1}): {error}")
                        
                except Exception as e:
                    raised = not is_permanent_error(e)  # a 400 (e.g. too long) won't change
                    last_error = str(e)
                    logger.warning(f"Error extracting metrics for paper {idx}, task {task_type} (attempt {attempt + 1}): {e}")
                
                if attempt < max_attempts - 1:
                    time.sleep(min(2.0 * (2 ** attempt), 10.0) + random.random() * 0.25)
            logger.warning(f"Failed to extract metrics for paper {idx}, task {task_type} after {max_attempts} attempts: {last_error}")
            return {"error": str(last_error or "no valid response")[:300], "raised": raised}

        # Checkpointed per paper and task; a call that raised is retried.
        task_key = lambda w: f"{dedupe_key(w[2])}|{w[1]}"  # noqa: E731
        done, failing = run_items(
            "1.8_metrics", extraction_tasks, key=task_key, fn=_extract_one,
            runner=thread_runner(_threads(), "Extracting metrics"),
            failed=lambda res: bool(res.get("raised")),
            fp=fingerprint(prompt_template, metrics_config, schemas, model_name))
        last = {task_key(w): res for w, res in failing}
        for work in extraction_tasks:
            idx, task_type, paper = work
            res = done.get(task_key(work)) or last.get(task_key(work))
            res = res if isinstance(res, dict) else {"error": str(getattr(res, "error", "no valid response"))}
            if "records" in res:
                results.extend(res["records"])
            else:
                _failed("1.8_metrics", paper, res["error"], task_type)

    if output_path:
        write_json(output_path, results)
        logger.info(f"Metrics extraction results saved to {output_path}")
    
    return results


def validate_metrics(metrics_records: List[Dict]) -> List[Dict]:
    """
    Validate extracted metrics and filter out empty or invalid records.
    
    Args:
        metrics_records: List of metric records
    
    Returns:
        Filtered list of valid metric records
    """
    valid_records = []
    
    for record in metrics_records:
        metrics = record.get('metrics', {})
        if not metrics:
            continue
        
        # Handle case where metrics is a list (multiple datasets per paper)
        if isinstance(metrics, list):
            logger.warning(f"Paper {record.get('paper_id')} has metrics as list (multiple datasets). Using first entry.")
            if not metrics:
                continue
            # Take the first entry or flatten
            if isinstance(metrics[0], dict) and 'results' in metrics[0]:
                # Format: [{"dataset_name": "...", "results": {...}}]
                metrics = metrics[0].get('results', {})
            else:
                metrics = metrics[0]
            record['metrics'] = metrics
        
        if not isinstance(metrics, dict):
            logger.warning(f"Paper {record.get('paper_id')} has invalid metrics format: {type(metrics)}")
            continue
        
        # Check if at least one metric value is non-empty
        has_value = False
        for key, value in metrics.items():
            if value and str(value).strip() and str(value).strip().lower() != 'nan':
                has_value = True
                break
        
        if has_value:
            # Normalize metric values
            normalized_metrics = {}
            for key, value in metrics.items():
                if value and str(value).strip() and str(value).strip().lower() != 'nan':
                    # Try to convert to float if it looks like a number
                    val_str = str(value).strip()
                    try:
                        # Handle percentages
                        if '%' in val_str:
                            val_str = val_str.replace('%', '').strip()
                            float_val = float(val_str) / 100.0
                            normalized_metrics[key] = str(float_val)
                        else:
                            float_val = float(val_str)
                            normalized_metrics[key] = val_str
                    except ValueError:
                        # Keep as string if not a number
                        normalized_metrics[key] = val_str
                else:
                    normalized_metrics[key] = None
            
            record['metrics'] = normalized_metrics
            valid_records.append(record)
    
    logger.info(f"Validated {len(valid_records)} out of {len(metrics_records)} metric records")
    return valid_records
