import os
import json
import click
from pathlib import Path
from dotenv import load_dotenv
from urllib.parse import urlparse
import shutil
import stat
import psycopg
import sys
from datetime import datetime
from contextlib import contextmanager

from .utils import atomic_json, read_lines, read_json, write_json, get_logger, configure_logging, add_file_logging, utf8_stdio

from .paper_code.search import QUERY_LOG, run_all_searches, write_query_log
from .paper_code.dedupe import PaperIndex, remove_duplicates, dedupe_merge, split_seen
from .paper_code.citations import check_citations_of_top
from .paper_code.filters import run_fulltext_filters
from .paper_code.merge import summarize_and_extract
from .paper_code.classify import (
    build_dataset_lists,
    search_papers_for_datasets,
    run_1_1_to_1_4,
)

logger = get_logger(__name__)
from .push_to_supabase import push_to_supabase
from .utils_metrics import MetricsRecorder
from .run_manifest import add_note, begin_run, end_run


class _TeeStream:
    def __init__(self, original, file_handle):
        self._original = original
        self._file = file_handle

    def write(self, data):
        self._original.write(data)
        self._original.flush()
        self._file.write(data)
        self._file.flush()

    def flush(self):
        self._original.flush()
        self._file.flush()

    def isatty(self):
        try:
            return self._original.isatty()
        except Exception:
            return False


@contextmanager
def _tee_stdout_stderr(log_path: Path):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(log_path, "a", encoding="utf-8")
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = _TeeStream(old_out, f)
    sys.stderr = _TeeStream(old_err, f)
    try:
        yield
    finally:
        sys.stdout = old_out
        sys.stderr = old_err
        try:
            f.close()
        except Exception:
            pass


def _clear_readonly_and_retry(func, path, _exc) -> None:
    """rmtree's error handler: Windows won't delete a read-only file; one still open
    elsewhere is reported and left, rather than silently blocking the next run."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError as e:
        click.echo(f"  could not remove {path}: {e}", err=True)


def _unresolved_host(e: Exception) -> bool:
    """The DB host name didn't resolve (macOS/Linux wording, then Windows')."""
    return any(m in str(e) for m in ("nodename nor servname", "Name or service not known",
                                     "getaddrinfo failed", "No such host is known"))


def _default_log_file(command_name: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return str(Path("pipeline/data/logs") / f"{command_name}_{ts}.log")


@click.group()
def cli():
    """Main entrypoint for the WSI Hub pipeline."""
    utf8_stdio()

@cli.command()
@click.option('--top-n', default=500, show_default=True, help='How many of the most-cited relevant initial-search papers seed the citation search (Step 1.3).')
@click.option('--max-terms', type=int, default=None, help='Limit how many search terms from pipeline/config/supp/search_terms.txt are used for Step 1.1.')
@click.option('--per-page', type=int, default=10, show_default=True, help='SerpAPI results per page (1-20). Keep 10: with 20, Scholar returns nothing after the first page.')
@click.option('--max-results', 'max_results_cap', type=int, default=100, show_default=True, help='Hard cap on total results per search term or cites_id. Scholar returns at most ~100 per query (rank 100+ comes back empty), so more buys nothing.')
@click.option('--max-pages', type=int, default=None, help='Optional maximum number of pages to fetch per search.')
@click.option('--page-sleep', type=float, default=0.5, show_default=True, help='Seconds to sleep between fetching pages within a search.')
@click.option('--max-dataset-seeds', type=int, default=None, help='Limit how many dataset seeds are searched in Step 1.6 (for testing).')
@click.option('--max-papers', type=int, default=None, help='Limit how many papers are used for Step 2 dataset searching.')
@click.option('--skip-step1', is_flag=True, default=False, help='Skip Step 1 (paper search/extraction) and proceed directly to Step 2.')
@click.option('--skip-dataset-dedupe', is_flag=True, default=False, help='Skip LLM-based dataset deduplication step.')
@click.option('--gem', is_flag=True, default=False, help='Use Gemini models instead of GPT models for LLM calls.')
@click.option('--perplexity', is_flag=True, default=False, help='Enable Perplexity AI searches (Step 1/2 LLM search providers). Default off.')
@click.option('--log/--no-log', 'log', default=False, show_default=True, help='Enable verbose logging (HTTP requests, library logs). Default off.')
@click.option('--log-file', 'log_file', default=None, show_default=False, help='Optional path to write console output + logs. Defaults to pipeline/data/logs/<command>_<timestamp>.log')
@click.option('--metrics/--no-metrics', 'metrics', default=True, show_default=True, help='Enable writing per-step metrics summary to a file.')
@click.option('--metrics-path', 'metrics_path', default='pipeline/data/metrics.json', show_default=True, help='Path to write metrics JSON summary file.')
@click.option('--use-batch', is_flag=True, default=False, help='Use batch API for LLM calls (50% cost reduction, resumable if interrupted).')
@click.option('--browser', is_flag=True, default=False, help='Use headless browser fallback for failed HTTP requests (requires: pip install playwright && playwright install chromium).')
@click.option('--run-id', default=None, help='Resume the run with this id. A new run needs an empty pipeline/data; one holding another run\'s outputs is refused, since every stage would reuse them.')
def papers(top_n, max_terms, per_page, max_results_cap, max_pages, page_sleep, max_dataset_seeds, max_papers, skip_step1, skip_dataset_dedupe, gem, perplexity, log, log_file, metrics, metrics_path, use_batch, browser, run_id):
    """Run the full paper processing pipeline."""
    load_dotenv()
    # Configure logging verbosity early
    configure_logging(verbose=log)
    log_path = Path(log_file) if log_file else Path(_default_log_file("papers"))
    add_file_logging(str(log_path), verbose=log)
    click.echo(f"Logging to: {log_path}")
    data = Path("pipeline/data")
    options = {k: v for k, v in locals().items() if k not in ("data", "log_path")}
    # Every finished item is checkpointed (pipeline.src.checkpoint), and every call and
    # fetch goes to the usage log, which `cli monitor` reads during the run.
    os.environ.setdefault("WSI_CHECKPOINT_DIR", str(data / "checkpoints"))
    os.environ.setdefault("WSI_USAGE_LOG", str(data / "usage_log.jsonl"))
    # What the stages saw (gate texts, SerpAPI pages, prompts; pipeline.src.provenance) and
    # one line per LLM call (pipeline.utils.llms.llm), for auditing the run afterwards.
    os.environ.setdefault("WSI_ARCHIVE_DIR", str(data / "archive"))
    os.environ.setdefault("WSI_LLM_LEDGER", str(data / "llm_calls.jsonl"))
    begin_run(data, run_id, options)
    with _tee_stdout_stderr(log_path):
        try:
            _run_papers(
                top_n, max_terms, per_page, max_results_cap, max_pages, page_sleep,
                max_dataset_seeds, max_papers, skip_step1, skip_dataset_dedupe,
                gem, perplexity, log, metrics, metrics_path, use_batch, browser
            )
        except BaseException as e:
            end_run(data, "failed", f"{type(e).__name__}: {e}"[:500])
            raise
        end_run(data, "completed")


@cli.command("keep-going", context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
@click.option('--max-restarts', type=int, default=20, show_default=True, help='Give up after this many restarts.')
@click.pass_context
def keep_going(ctx, max_restarts):
    """Run `papers` with the given options, and resume it whenever it stops on an error.

    \b
    python -m pipeline.src.cli keep-going --max-terms 20   # any `papers` options
    """
    from .supervise import supervise
    sys.exit(supervise(list(ctx.args), max_restarts=max_restarts))


@cli.command()
@click.argument('text')
@click.option('--minutes', type=float, default=None, help="Time you spent on this, for the run's human-time total.")
@click.option('--openai-usd', type=float, default=None, help='OpenAI spend or balance read off the dashboard.')
@click.option('--serper-credits', type=float, default=None, help='Serper credits left.')
@click.option('--other', default=None, help='Any other reading, e.g. "openalex credits 8200".')
def note(text, minutes, openai_usd, serper_credits, other):
    """Add a dated note to the run manifest: a balance, or time spent stepping in.

    \b
    python -m pipeline.src.cli note "OpenAI balance before launch" --openai-usd 412.50
    python -m pipeline.src.cli note "restarted after the IEEE block" --minutes 15
    """
    click.echo(json.dumps(add_note(Path("pipeline/data"), text, minutes, openai_usd=openai_usd,
                                   serper_credits=serper_credits, other=other)))


@cli.command("verify-run")
@click.option('--data', 'data_dir', default="pipeline/data", show_default=True, help='The run\'s data root.')
@click.option('--deep', is_flag=True, default=False, help='Also decompress every archived object and check its hash.')
def verify_run_cmd(data_dir, deep):
    """Check the run's evidence is all there (provenance.verify_run): every archived object
    the records point at, the LLM ledger and usage log call by call, no lost writes or
    cut-off lines. Exits 1 if not. Run it after the workers stop."""
    from .provenance import verify_run
    report = verify_run(Path(data_dir), deep=deep)
    click.echo(json.dumps(report, indent=1))
    sys.exit(0 if report["complete"] else 1)






@cli.command()
@click.option('--log', 'log_path', default=None, help='Usage log to read (default: WSI_USAGE_LOG, else pipeline/data/usage_log.jsonl).')
@click.option('--window', type=float, default=60, show_default=True, help='Minutes of recent activity the per-site table covers.')
@click.option('--watch', type=float, default=0, help='Print again every this many minutes (0: once).')
@click.option('--notify', is_flag=True, default=False, help='Also show a macOS notification for each new alert.')
def monitor(log_path, window, watch, notify):
    """Full-text share per hour, per-site blocks and cool-downs, and alerts, for a running pipeline."""
    import time as _time
    from .monitor import alerts, notify as _notify, read_events, report
    path = Path(log_path or os.getenv("WSI_USAGE_LOG") or "pipeline/data/usage_log.jsonl")
    seen = set()
    while True:
        click.echo(report(path, window_min=window))
        if notify:
            for a in alerts(read_events(path), _time.time()):
                key = a.split(" in the last")[0].split(" is cooling")[0]
                if key not in seen:
                    seen.add(key)
                    _notify(a)
        if not watch:
            return
        _time.sleep(watch * 60)


def _run_papers(top_n, max_terms, per_page, max_results_cap, max_pages, page_sleep, max_dataset_seeds, max_papers, skip_step1, skip_dataset_dedupe, gem, perplexity, log, metrics, metrics_path, use_batch, browser):
    load_dotenv()
    configure_logging(verbose=log)
    # Lazy import validators to avoid schema loading at module import time
    from .paper_code.validators import validate_and_normalize_extraction, validate_and_normalize_classification
    if not os.getenv("SERPAPI_API_KEY"):
        click.echo("Warning: SERPAPI_API_KEY is not set. Searches will likely return 0 results.")
    DATA = Path("pipeline/data")
    PAPER_DATA = DATA / "paper_data"
    PROMPTS = Path("pipeline/config/prompts")

    DATA.mkdir(parents=True, exist_ok=True)
    PAPER_DATA.mkdir(parents=True, exist_ok=True)

    # Step 1: Paper processing (can be skipped)
    metrics_rec = MetricsRecorder(Path(metrics_path)) if metrics else None
    if not skip_step1:
        # Step 1.1: Initial Search
        click.echo("Step 1.1: Running initial search...")
        search_terms_path = "pipeline/config/supp/search_terms.txt"
        results_path = PAPER_DATA / "results.json"

        if not results_path.exists():
            search_terms = read_lines(search_terms_path)
            if max_terms is not None and max_terms > 0:
                search_terms = search_terms[:max_terms]
            click.echo(f"Loaded {len(search_terms)} search terms from {search_terms_path}")
            results = run_all_searches(
                search_terms,
                out=str(results_path),
                per_page=per_page,
                max_results_cap=max_results_cap,
                max_pages=max_pages,
                page_sleep=page_sleep,
            )
            click.echo(f"Initial search returned {len(results)} results (pre-dedupe/filter)")
            if metrics_rec:
                metrics_rec.record(step="1.1_initial_search", entity="papers", output_count=len(results))
        else:
            click.echo(f"Found existing results at {results_path}, loading them.")
            results = read_json(results_path)
            click.echo(f"Loaded {len(results)} existing results")
            if metrics_rec:
                metrics_rec.record(step="1.1_initial_search", entity="papers", output_count=len(results), extra={"cache": True})

        results = remove_duplicates(results)
        # Every paper the initial search surfaced has already been judged by the
        # gates below, accepted or not. Remembered so the citation search does not
        # pay to judge it again.
        initial_seen = PaperIndex(results)
        gate_prompt = PROMPTS / "paper_prompts" / "check_all_criteria_structured.md"
        click.echo(f"Initial search produced {len(results)} papers after filtering.")

        click.echo("")
        # Step 1.2: Full-text filtering on initial search results
        click.echo("Step 1.2: Running full-text filtering on initial results...")
        fulltext_initial_path = PAPER_DATA / "fulltext_pass_initial.json"
        if not fulltext_initial_path.exists():
            filtered_initial = run_fulltext_filters(
                items=results,
                prompt_path=gate_prompt,
                out=str(fulltext_initial_path),
                use_gemini=gem,
                use_batch=use_batch,
                use_browser=browser,
                step_name="fulltext_filter_1_2",
            )
            if metrics_rec:
                metrics_rec.record(step="1.2_fulltext_initial", entity="papers", input_count=len(results), output_count=len(filtered_initial))
        else:
            click.echo(f"Found existing initial full-text filtered papers at {fulltext_initial_path}, loading them.")
            filtered_initial = read_json(fulltext_initial_path)
            if metrics_rec:
                metrics_rec.record(step="1.2_fulltext_initial", entity="papers", output_count=len(filtered_initial), extra={"cache": True})
        click.echo(f"{len(filtered_initial)} papers passed initial full-text filtering.")

        click.echo("")
        # Step 1.3: Citation Search (on top-N of filtered initial set)
        click.echo("Step 1.3: Running citation search on filtered top-N...")
        # The N most-cited relevant papers (this used to be the first N in search-term order).
        top_results = sorted(filtered_initial, key=lambda p: -(p.get('citations') or 0))[:top_n]
        citations_path = PAPER_DATA / "citations_from_filtered.json"

        # Cache: citation search can be slow; reuse if already computed
        if citations_path.exists():
            click.echo(f"Found existing citations at {citations_path}, loading them.")
            citations = read_json(citations_path)
        else:
            citations = check_citations_of_top(
                top_results,
                out=str(citations_path),
                per_page=per_page,
                max_results_cap=max_results_cap,
                max_pages=max_pages,
                page_sleep=page_sleep,
            )
        citations = remove_duplicates(citations)
        # In the January run 39% of citation results were already in the initial
        # search, and half the papers this step "accepted" were already accepted.
        # Drop them before filtering so each paper is judged once.
        pre_seen = len(citations)
        citations, seen = split_seen(citations, initial_seen)
        atomic_json(PAPER_DATA / "citations_already_seen.json", seen, indent=1)
        click.echo(f"Citation search: {pre_seen - len(citations)} of {pre_seen} results were "
                   f"already judged in the initial search; skipping them.")
        click.echo(f"Citation search found {len(citations)} new papers.")

        # Full-text filtering on citation results
        click.echo("Step 1.3b: Running full-text filtering on citation results...")
        fulltext_citations_path = PAPER_DATA / "fulltext_pass_citations.json"
        if not fulltext_citations_path.exists():
            filtered_citations = run_fulltext_filters(
                items=citations,
                prompt_path=gate_prompt,
                out=str(fulltext_citations_path),
                use_gemini=gem,
                use_batch=use_batch,
                use_browser=browser,
                step_name="fulltext_filter_1_3b",
            )
            if metrics_rec:
                metrics_rec.record(step="1.3b_fulltext_citations", entity="papers", input_count=len(citations), output_count=len(filtered_citations))
        else:
            click.echo(f"Found existing citation full-text filtered papers at {fulltext_citations_path}, loading them.")
            filtered_citations = read_json(fulltext_citations_path)
            if metrics_rec:
                metrics_rec.record(step="1.3b_fulltext_citations", entity="papers", output_count=len(filtered_citations), extra={"cache": True})
        click.echo(f"{len(filtered_citations)} citation-derived papers passed full-text filtering.")

        # Combine filtered initial results and filtered citations
        combined = dedupe_merge(filtered_initial, filtered_citations)
        combined_path = PAPER_DATA / "combined.json"
        write_json(combined_path, combined)
        click.echo(f"Combined and deduplicated list has {len(combined)} papers.")
        if metrics_rec:
            metrics_rec.record(step="1.combined", entity="papers", output_count=len(combined))

        click.echo("")
        # Step 1.4: Summarization and Extraction
        click.echo("Step 1.4: Summarizing and extracting data from papers...")
        prompts_to_run = [
            "sum_paper.md",
            "paper_classification_prompt.md",
            "dataset_details.md",
        ]
        paper_prompts_dir = PROMPTS / "paper_prompts"
        extracted_path = PAPER_DATA / "extracted.json"

        if not extracted_path.exists():
            extracted_data = summarize_and_extract(
                items=combined,
                prompts=prompts_to_run,
                prompts_dir=paper_prompts_dir,
                out=extracted_path,
                use_gemini=gem,
                use_batch=use_batch
            )
            if metrics_rec:
                metrics_rec.record(step="1.4_extraction", entity="papers", input_count=len(combined), output_count=len(extracted_data))
        else:
            click.echo(f"Found existing extracted data at {extracted_path}, loading them.")
            extracted_data = read_json(extracted_path)
            if metrics_rec:
                metrics_rec.record(step="1.4_extraction", entity="papers", output_count=len(extracted_data), extra={"cache": True})

        # Backfill: if new prompts are missing in cached data, run them only for missing items and merge
        needed_new_prompts = {"dataset_details.md"}
        missing_items = []
        for it in extracted_data:
            ex = it.get("extraction", {}) or {}
            missing = any(p not in ex or not ex.get(p) for p in needed_new_prompts)
            if missing:
                missing_items.append(it)

        did_backfill = bool(missing_items)
        if missing_items:
            click.echo(f"Detected {len(missing_items)} papers missing new extraction prompts; running backfill for dataset details...")
            backfill_prompts = [p for p in prompts_to_run if p in needed_new_prompts]
            backfill_results = summarize_and_extract(
                items=missing_items,
                prompts=backfill_prompts,
                prompts_dir=paper_prompts_dir,
                out=extracted_path.with_name(extracted_path.stem + "_backfill.json"),
                use_gemini=gem,
                use_batch=use_batch
            )

            # Merge backfill results into extracted_data keyed by a stable identifier
            def _key(rec):
                return rec.get("dedupe_key") or rec.get("link") or rec.get("url") or rec.get("title")

            backfill_map = {_key(it): it for it in backfill_results}
            for i, it in enumerate(extracted_data):
                k = _key(it)
                if k in backfill_map:
                    ex = it.get("extraction", {}) or {}
                    ex_b = backfill_map[k].get("extraction", {}) or {}
                    # Only add missing prompt outputs
                    for p in needed_new_prompts:
                        if p in ex_b and (p not in ex or not ex.get(p)):
                            ex[p] = ex_b[p]
                    it["extraction"] = ex
                    extracted_data[i] = it

            # Persist merged data
            write_json(extracted_path, extracted_data)

        # Validate and normalize the extracted data
        for item in extracted_data:
            if 'extraction' in item and item['extraction']:
                # Validate paper extraction
                if 'sum_paper.md' in item['extraction']:
                    item['extraction']['sum_paper.md'] = validate_and_normalize_extraction(
                        item['extraction']['sum_paper.md']
                    )
            
                # Validate paper classification
                if 'paper_classification_prompt.md' in item['extraction']:
                    paper_year = item.get('year')
                    item['extraction']['paper_classification_prompt.md'] = validate_and_normalize_classification(
                        item['extraction']['paper_classification_prompt.md'],
                        paper_year=paper_year
                    )

        write_json(extracted_path, extracted_data)
        click.echo(f"Finished summarization and extraction. Enriched data saved to {extracted_path}")

        click.echo("")
        # Step 1.5: Build dataset lists
        click.echo("Step 1.5: Building dataset seed list...")
        datasets_seed_path = PAPER_DATA / "datasets_seed.json"
        if not datasets_seed_path.exists():
            dataset_seed = build_dataset_lists(
                premade_path="pipeline/config/supp/initial_dataset_list.csv",
                from_papers=extracted_data,
                out=str(datasets_seed_path)
            )
            if metrics_rec:
                metrics_rec.record(step="1.5_build_dataset_seed", entity="datasets", input_count=len(extracted_data), output_count=len(dataset_seed))
        else:
            click.echo(f"Found existing dataset seed list at {datasets_seed_path}, loading it.")
            dataset_seed = read_json(datasets_seed_path)["datasets"]
            if did_backfill or len(dataset_seed) == 0:
                click.echo("Refreshing dataset seed list due to backfill or empty seed...")
                dataset_seed = build_dataset_lists(
                    premade_path="pipeline/config/supp/initial_dataset_list.csv",
                    from_papers=extracted_data,
                    out=str(datasets_seed_path)
                )
        if metrics_rec:
            metrics_rec.record(step="1.5_build_dataset_seed", entity="datasets", output_count=len(dataset_seed), extra={"cache": datasets_seed_path.exists()})
        click.echo(f"Built dataset seed list with {len(dataset_seed)} datasets.")

        # Step 1.5b: Deduplicate dataset slugs using LLM
        if not skip_dataset_dedupe:
            click.echo("Step 1.5b: Deduplicating dataset slugs...")
            deduped_path = PAPER_DATA / "datasets_seed_deduped.json"
        
            if not deduped_path.exists():
                from .dataset_code.dataset_canonical import canonicalize_datasets, to_json
                canonical = to_json(canonicalize_datasets(
                    dataset_seed, papers=extracted_data, use_gemini=gem))
                # One record per canonical dataset, plus the variant -> canonical map
                write_json(deduped_path, canonical)
                dataset_seed = canonical["datasets"]
            else:
                click.echo(f"Found existing deduplicated dataset list at {deduped_path}, loading it.")
                dataset_seed = read_json(deduped_path)["datasets"]
        
            removed_count = len(read_json(datasets_seed_path)["datasets"]) - len(dataset_seed)
            click.echo(f"Deduplication removed {removed_count} duplicate dataset slugs. Final count: {len(dataset_seed)} datasets.")
            if metrics_rec:
                metrics_rec.record(step="1.5b_dataset_dedupe", entity="datasets", output_count=len(dataset_seed), extra={"removed": removed_count})
        else:
            click.echo("Skipping dataset deduplication (--skip-dataset-dedupe flag set).")

        click.echo("")
        # Step 1.6: Dataset-focused paper search
        click.echo("Step 1.6: Running dataset-focused paper search...")
        dataset_papers_path = PAPER_DATA / "dataset_papers.json"
        if not dataset_papers_path.exists():
            if max_dataset_seeds is not None and max_dataset_seeds > 0:
                click.echo(f"Limiting dataset seeds to first {max_dataset_seeds} (testing mode)")
                dataset_seed = dataset_seed[:max_dataset_seeds]
            QUERY_LOG.clear()
            dataset_search_results = search_papers_for_datasets(dataset_seed)
            for item in dataset_search_results:
                item['source'] = 'dataset_search'
            write_query_log(str(dataset_papers_path))
            dataset_papers = run_1_1_to_1_4(
                seed_results=dataset_search_results,
                prompts_dir=paper_prompts_dir,
                out=str(dataset_papers_path),
                use_gemini=gem,
                already_processed=extracted_data,
                use_batch=use_batch,
                use_browser=browser,
            )
            if metrics_rec:
                metrics_rec.record(step="1.6_dataset_focused_search", entity="papers", input_count=len(dataset_search_results), output_count=len(dataset_papers))
        else:
            click.echo(f"Found existing dataset papers at {dataset_papers_path}, loading them.")
            dataset_papers = read_json(dataset_papers_path)
            if metrics_rec:
                metrics_rec.record(step="1.6_dataset_focused_search", entity="papers", output_count=len(dataset_papers), extra={"cache": True})
        click.echo(f"Found {len(dataset_papers)} new papers from dataset-focused search.")
    
        # Merge extracted.json and dataset_papers.json into final_extracted.json
        click.echo("")
        click.echo("Merging all paper extractions into final_extracted.json...")
        final_out_path = PAPER_DATA / "final_extracted.json"
        def _merge_records(base: dict, incoming: dict) -> dict:
            if not base:
                return incoming
            b_ex = base.get("extraction") or {}
            i_ex = incoming.get("extraction") or {}
            if not b_ex and i_ex:
                base["extraction"] = i_ex
            elif b_ex and i_ex:
                for k, v in i_ex.items():
                    if k not in b_ex or not b_ex.get(k):
                        b_ex[k] = v
                base["extraction"] = b_ex
            for fld in ("title", "year", "authors", "link", "pdf_url"):
                if not base.get(fld) and incoming.get(fld):
                    base[fld] = incoming[fld]
            return base
        # One record per paper, also across URLs (PaperIndex): the extracted record keeps
        # its fields and takes what the dataset-search copy adds.
        final_list = remove_duplicates(list(extracted_data) + list(dataset_papers), merge=_merge_records)

        # Normalize/validate merged records so downstream steps see clean shapes
        from .paper_code.validators import (
            validate_and_normalize_extraction,
            validate_and_normalize_classification,
        )
        for item in final_list:
            ex = item.get("extraction", {}) or {}
            if ex:
                if 'sum_paper.md' in ex:
                    ex['sum_paper.md'] = validate_and_normalize_extraction(ex.get('sum_paper.md') or {})
                if 'paper_classification_prompt.md' in ex:
                    ex['paper_classification_prompt.md'] = validate_and_normalize_classification(
                        ex.get('paper_classification_prompt.md') or {},
                        paper_year=item.get('year')
                    )
                item['extraction'] = ex

        write_json(final_out_path, final_list)
        click.echo(f"Wrote merged final file with {len(final_list)} unique papers to {final_out_path}")
        if metrics_rec:
            metrics_rec.record(step="1.merge_final_extracted", entity="papers", output_count=len(final_list))

        click.echo("")
        # Step 1.7: Task Classification
        click.echo("Step 1.7: Classifying papers by task type...")
        from .paper_code.metrics_extractor import classify_paper_tasks
        
        task_classified_path = PAPER_DATA / "task_classified.json"
        task_prompt_path = PROMPTS / "paper_prompts" / "task_classification.md"
        task_schema_path = Path("pipeline/config/schemas/task_classification_schema.json")
        
        if not task_classified_path.exists():
            task_classified_papers = classify_paper_tasks(
                papers=final_list,
                prompt_path=task_prompt_path,
                schema_path=task_schema_path,
                use_gemini=gem,
                use_batch=use_batch,
                output_path=task_classified_path
            )
            if metrics_rec:
                # Count papers with at least one task
                papers_with_tasks = sum(1 for p in task_classified_papers 
                                       if any(p.get('task_classification', {}).values()))
                metrics_rec.record(step="1.7_task_classification", entity="papers", 
                                 input_count=len(final_list), output_count=papers_with_tasks)
        else:
            click.echo(f"Found existing task classification at {task_classified_path}, loading it.")
            task_classified_papers = read_json(task_classified_path)
            if metrics_rec:
                papers_with_tasks = sum(1 for p in task_classified_papers 
                                       if any(p.get('task_classification', {}).values()))
                metrics_rec.record(step="1.7_task_classification", entity="papers", 
                                 output_count=papers_with_tasks, extra={"cache": True})
        
        # Count task types
        task_counts = {"classification": 0, "segmentation": 0, "detection": 0, "regression": 0, "survival": 0}
        for paper in task_classified_papers:
            tasks = paper.get('task_classification', {})
            for task_type in task_counts:
                if tasks.get(task_type, False):
                    task_counts[task_type] += 1
        
        click.echo(f"Task classification complete:")
        for task_type, count in task_counts.items():
            if count > 0:
                click.echo(f"  - {task_type}: {count} papers")

        click.echo("")
        # Step 1.8: Metrics Extraction
        click.echo("Step 1.8: Extracting performance metrics...")
        from .paper_code.metrics_extractor import extract_metrics_for_papers, validate_metrics
        
        metrics_extracted_path = PAPER_DATA / "metrics_extracted.json"
        metrics_config_path = Path("pipeline/config/schemas/metrics/metrics_config.json")
        metrics_prompt_path = PROMPTS / "paper_prompts" / "metrics.md"
        metrics_schemas_dir = Path("pipeline/config/schemas/metrics")
        
        if not metrics_extracted_path.exists():
            extracted_metrics = extract_metrics_for_papers(
                papers=task_classified_papers,
                metrics_config_path=metrics_config_path,
                metrics_prompt_path=metrics_prompt_path,
                schemas_dir=metrics_schemas_dir,
                use_gemini=gem,
                use_batch=use_batch,
                output_path=metrics_extracted_path
            )
            
            # Validate metrics
            validated_metrics = validate_metrics(extracted_metrics)
            
            # Save validated metrics
            metrics_final_path = PAPER_DATA / "metrics_final.json"
            write_json(metrics_final_path, validated_metrics)
            
            if metrics_rec:
                metrics_rec.record(step="1.8_metrics_extraction", entity="metrics", 
                                 input_count=len(extracted_metrics), output_count=len(validated_metrics))
            click.echo(f"Extracted and validated {len(validated_metrics)} metric records")
        else:
            click.echo(f"Found existing metrics at {metrics_extracted_path}, loading them.")
            extracted_metrics = read_json(metrics_extracted_path)
            validated_metrics = validate_metrics(extracted_metrics)
            
            # Ensure final file exists
            metrics_final_path = PAPER_DATA / "metrics_final.json"
            if not metrics_final_path.exists():
                write_json(metrics_final_path, validated_metrics)
            
            if metrics_rec:
                metrics_rec.record(step="1.8_metrics_extraction", entity="metrics", 
                                 output_count=len(validated_metrics), extra={"cache": True})
            click.echo(f"Loaded {len(validated_metrics)} validated metric records")

        from .paper_code.metrics_extractor import FAILED_CALLS
        if FAILED_CALLS:
            write_json(PAPER_DATA / "failed_calls.json", FAILED_CALLS)
            click.echo(f"{len(FAILED_CALLS)} task/metrics calls failed for good (an error, not 'no tasks' "
                       f"or 'no results'): {PAPER_DATA / 'failed_calls.json'}")
        if metrics_rec:
            metrics_rec.record(step="1.failed_calls", entity="calls", output_count=len(FAILED_CALLS))

        click.echo("")
        click.echo("Step 1 finished.")
    else:
        click.echo("Skipping Step 1 (paper search/extraction) as requested.")
    
    click.echo("")
    # Step 2: Dataset searching (paper-to-dataset matching and web search)
    click.echo("Step 2: Running dataset search pipeline...")
    
    # Check if we have papers to work with
    final_extracted_path = PAPER_DATA / "final_extracted.json"
    if not final_extracted_path.exists():
        click.echo("No extracted papers found. Cannot run Step 2 without papers.")
        click.echo("Please run without --skip-step1 first.")
        return
    
    # Load papers
    papers = read_json(final_extracted_path)
    if max_papers:
        papers = papers[:max_papers]
    click.echo(f"Loaded {len(papers)} papers from final_extracted.json")
    if metrics_rec:
        metrics_rec.record(step="2.load_papers", entity="papers", output_count=len(papers))
    
    # Step 2.1: Generate paper-dataset matches
    from .dataset_code.paper_dataset_matcher import (
        generate_paper_dataset_matches,
        get_unique_datasets_from_matches
    )
    
    DATASET_DATA = DATA / "dataset_data"
    DATASET_DATA.mkdir(parents=True, exist_ok=True)
    
    matches_path = DATASET_DATA / "paper_to_dataset_matches.jsonl"
    data_match_prompt = PROMPTS / "dataset_prompts" / "map_extracted_datasets.md"
    
    # Build dataset pool: prefer the canonical list from 1.5b; fall back to the
    # raw seed list, then the premade CSV.
    dataset_pool: list[str] = []
    canonical_records: dict = {}
    dedup_path = PAPER_DATA / "datasets_seed_deduped.json"
    seed_path = PAPER_DATA / "datasets_seed.json"

    if dedup_path.exists():
        try:
            deduped = read_json(dedup_path)
            # 1.5b now writes one record per canonical dataset (name, variant
            # names, paper-written descriptions). Older files only had slugs.
            canonical_records = deduped.get("records") or {}
            dataset_pool = ([r.get("name") or slug for slug, r in canonical_records.items()]
                            if canonical_records else deduped.get("datasets", []))
        except Exception:
            dataset_pool = []
    elif seed_path.exists():
        try:
            dataset_pool = read_json(seed_path).get("datasets", [])
        except Exception:
            dataset_pool = []
    # Fallback to premade CSV if still empty
    if not dataset_pool:
        premade_csv = Path("pipeline/config/supp/initial_dataset_list.csv")
        if premade_csv.exists():
            try:
                rows = premade_csv.read_text(encoding="utf-8").splitlines()
                # Skip header if present; take first column as dataset slug/name
                if rows:
                    hdr = rows[0].lower()
                    start = 1 if ("dataset" in hdr or "," in hdr) else 0
                    for r in rows[start:]:
                        name = (r.split(",")[0] or "").strip()
                        if name:
                            dataset_pool.append(name)
            except Exception:
                dataset_pool = []
    # Normalize dataset names: add humanized variants for sluggy entries
    def _humanize(name: str) -> str:
        n = (name or "").strip()
        if not n:
            return n
        # Replace separators with spaces and collapse whitespace
        for ch in ("-", "_", "/"):
            n = n.replace(ch, " ")
        n = " ".join(n.split())
        # Uppercase common acronyms
        acronyms = {"tcga", "tcia", "camelyon", "brats", "panda"}
        parts = [p.upper() if p.lower() in acronyms else p.capitalize() for p in n.split(" ")]
        return " ".join(parts)

    # Canonical names are already readable, and their variants travel to the
    # matcher as aliases, so only legacy slug lists need humanized variants.
    if dataset_pool and not canonical_records:
        expanded: list[str] = []
        seen = set()
        for d in dataset_pool:
            if d and d not in seen:
                expanded.append(d)
                seen.add(d)
            h = _humanize(d)
            if h and h not in seen:
                expanded.append(h)
                seen.add(h)
        dataset_pool = expanded

    if dataset_pool:
        def _slugify_dataset_seed(name: str) -> str:
            return (name or "").strip().lower().replace(" ", "-").replace("_", "-")

        def _choose_better_name(a: str, b: str) -> str:
            a = (a or "").strip()
            b = (b or "").strip()
            a_has_space = " " in a
            b_has_space = " " in b
            if a_has_space != b_has_space:
                return a if a_has_space else b
            if len(a) != len(b):
                return a if len(a) > len(b) else b
            return a

        by_slug: dict[str, str] = {}
        for name in dataset_pool:
            slug = _slugify_dataset_seed(name)
            if not slug:
                continue
            if slug not in by_slug:
                by_slug[slug] = name
            else:
                by_slug[slug] = _choose_better_name(by_slug[slug], name)

        if by_slug and len(by_slug) != len(dataset_pool):
            click.echo(f"Deduplicated dataset seeds by slug: {len(dataset_pool)} -> {len(by_slug)}")
            dataset_pool = list(by_slug.values())

    # Apply cap
    if max_dataset_seeds is not None and max_dataset_seeds > 0:
        dataset_pool = dataset_pool[:max_dataset_seeds]
    if metrics_rec:
        metrics_rec.record(step="2.dataset_pool_built", entity="datasets", output_count=len(dataset_pool))

    # Step 2.0: validate datasets BEFORE matching, from what the papers already
    # say about them, so 2.1 and 2.2 never spend calls on non-pathology datasets.
    # Measured on the January catalog: given the paper descriptions, this keeps
    # 99% of what the full web-search validation accepts. Datasets with no
    # description are kept and left to the full validation in 2.2.
    alias_names: dict[str, set[str]] = {}
    if canonical_records:
        by_name = {(r.get("name") or slug): r for slug, r in canonical_records.items()}
        alias_names = {n: set(r.get("names") or []) for n, r in by_name.items()}
        if os.getenv("WSI_PREVALIDATE_POOL", "1").lower() not in ("0", "false", "no"):
            from .dataset_code.dataset_validator import prevalidate_datasets
            in_pool = set(dataset_pool)
            rejected = prevalidate_datasets(
                {n: r.get("descriptions") or [] for n, r in by_name.items() if n in in_pool},
                prompt_path=PROMPTS / "dataset_prompts" / "validate_pathology_dataset.md",
                cache_path=DATASET_DATA / "prevalidation.json",
                use_gemini=gem,
            )
            before = len(dataset_pool)
            dataset_pool = [d for d in dataset_pool if d not in rejected]
            click.echo(f"Step 2.0: pre-validation removed {before - len(dataset_pool)} non-pathology "
                       f"datasets; {len(dataset_pool)} remain")
            if metrics_rec:
                metrics_rec.record(step="2.0_prevalidation", entity="datasets",
                                   input_count=before, output_count=len(dataset_pool))

    # Step 2.1: Paper-dataset matching
    if not dataset_pool:
        click.echo("Dataset pool is empty. Provide seeds or run Step 1 first.")
        return

    # Check if matches already exist
    if matches_path.exists():
        click.echo(f"Step 2.1: Found existing matches at {matches_path}, loading them...")
        matches = []
        with open(matches_path, 'r', encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    matches.append(json.loads(line))
        click.echo(f"Loaded {len(matches)} existing paper-dataset matches")
    else:
        click.echo(f"Step 2.1: Matching {len(papers)} papers against {len(dataset_pool)} datasets...")
        matches = generate_paper_dataset_matches(
            papers=papers,
            prompt_path=data_match_prompt,
            output_path=matches_path,
            max_papers=None,  # Already limited above
            use_gemini=gem,
            dataset_names=dataset_pool,
            use_batch=use_batch,
            alias_names=alias_names,
        )
        click.echo(f"Found {len(matches)} paper-dataset matches")
    if metrics_rec:
        metrics_rec.record(step="2.1_paper_dataset_matches", entity="matches", output_count=len(matches), extra={"papers": len(papers), "datasets": len(dataset_pool)})
    
    click.echo("")
    # Step 2.2: Run dataset search pipeline
    from .dataset_code.dataset_search_orchestrator import (
        run_dataset_search_pipeline,
        build_datasets_seed_json
    )
    
    click.echo("Step 2.2: Running web search and metadata extraction for datasets...")
    
    dataset_prompts_dir = PROMPTS / "dataset_prompts"
    schema_path = Path("pipeline/config/schemas/dataset_fields_schema.json")
    
    results = run_dataset_search_pipeline(
        dataset_names=dataset_pool,
        base_dir=DATASET_DATA,
        prompts_dir=dataset_prompts_dir,
        schema_path=schema_path,
        use_gemini=gem,
        use_perplexity=perplexity,
        max_pages=10,
        num_workers=None,  # Auto-detect
        use_browser=browser,
        use_batch=use_batch,
        descriptions_by_name={(r.get("name") or s): r.get("descriptions") or []
                              for s, r in canonical_records.items()} if canonical_records else None,
    )
    if metrics_rec:
        completed = sum(1 for r in results if r.get('status') == 'completed')
        rejected = sum(1 for r in results if r.get('status') == 'rejected')
        errors = sum(1 for r in results if r.get('status') == 'error')
        metrics_rec.record(step="2.2_dataset_pipeline", entity="datasets", output_count=len(results), extra={"completed": completed, "rejected": rejected, "errors": errors})
    
    click.echo("")
    # Build datasets_seed.json from results (write to dataset_data folder)
    datasets_seed_output = DATASET_DATA / "datasets_seed_step2.json"
    if datasets_seed_output.exists():
        click.echo(f"Found existing {datasets_seed_output.name}, updating it...")
    parents_by_name = {}
    if canonical_records:
        from .dataset_code.dataset_canonical import assign_parents
        assign_parents(canonical_records)
        parents_by_name = {(r.get("name") or s): (canonical_records[r["parent"]].get("name") or r["parent"])
                           for s, r in canonical_records.items() if r.get("parent")}
    public_datasets = build_datasets_seed_json(
        results=results,
        base_dir=DATASET_DATA,
        output_path=datasets_seed_output,
        parents_by_name=parents_by_name,
    )
    if metrics_rec:
        metrics_rec.record(step="2.2_seed_build", entity="datasets", output_count=len(public_datasets))
    
    click.echo(f"Built {datasets_seed_output.name} with {len(public_datasets)} public datasets")
    click.echo("")
    click.echo("Pipeline finished.")
    if metrics_rec:
        try:
            summary_text = metrics_rec.render_text_summary()
            metrics_rec.write()
            click.echo(f"Metrics written to {metrics_path}")
            click.echo("")
            click.echo("Pipeline metrics summary:")
            click.echo(summary_text)
        except Exception:
            pass

@cli.command()
@click.option('--papers-json', type=click.Path(exists=True), default='pipeline/data/paper_data/final_extracted.json', show_default=True, help='Path to papers JSON file')
@click.option('--output', type=click.Path(), default='pipeline/data/paper_data/metrics_final.json', show_default=True, help='Path to save extracted metrics')
@click.option('--gem', is_flag=True, default=False, help='Use Gemini models instead of GPT models for LLM calls')
@click.option('--use-batch', is_flag=True, default=False, help='Use batch API for LLM calls (50% cost reduction, resumable if interrupted)')
@click.option('--log/--no-log', 'log', default=False, show_default=True, help='Enable verbose logging')
@click.option('--log-file', 'log_file', default=None, show_default=False, help='Optional path to write console output + logs. Defaults to pipeline/data/logs/<command>_<timestamp>.log')
def metrics(papers_json, output, gem, use_batch, log, log_file):
    """Extract task classification and performance metrics from papers."""
    load_dotenv()
    configure_logging(verbose=log)
    log_path = Path(log_file) if log_file else Path(_default_log_file("metrics"))
    add_file_logging(str(log_path), verbose=log)
    click.echo(f"Logging to: {log_path}")
    # The same capture as `papers` (usage log, LLM ledger, archive).
    data = Path("pipeline/data")
    os.environ.setdefault("WSI_USAGE_LOG", str(data / "usage_log.jsonl"))
    os.environ.setdefault("WSI_LLM_LEDGER", str(data / "llm_calls.jsonl"))
    os.environ.setdefault("WSI_ARCHIVE_DIR", str(data / "archive"))
    with _tee_stdout_stderr(log_path):
        _run_metrics(papers_json, output, gem, use_batch, log)


def _run_metrics(papers_json, output, gem, use_batch, log):
    load_dotenv()
    configure_logging(verbose=log)
    
    from .paper_code.metrics_extractor import classify_paper_tasks, extract_metrics_for_papers, validate_metrics
    
    PROMPTS = Path("pipeline/config/prompts")
    
    click.echo(f"Loading papers from {papers_json}...")
    papers = read_json(papers_json)
    click.echo(f"Loaded {len(papers)} papers")
    
    # Step 1: Task Classification
    click.echo("\nStep 1: Classifying papers by task type...")
    task_classified_path = Path(papers_json).parent / "task_classified.json"
    task_prompt_path = PROMPTS / "paper_prompts" / "task_classification.md"
    task_schema_path = Path("pipeline/config/schemas/task_classification_schema.json")
    
    task_classified_papers = classify_paper_tasks(
        papers=papers,
        prompt_path=task_prompt_path,
        schema_path=task_schema_path,
        use_gemini=gem,
        use_batch=use_batch,
        output_path=task_classified_path
    )
    
    # Count task types
    task_counts = {"classification": 0, "segmentation": 0, "detection": 0, "regression": 0, "survival": 0}
    for paper in task_classified_papers:
        tasks = paper.get('task_classification', {})
        for task_type in task_counts:
            if tasks.get(task_type, False):
                task_counts[task_type] += 1
    
    click.echo("Task classification complete:")
    for task_type, count in task_counts.items():
        if count > 0:
            click.echo(f"  - {task_type}: {count} papers")
    
    # Step 2: Metrics Extraction
    click.echo("\nStep 2: Extracting performance metrics...")
    metrics_extracted_path = Path(papers_json).parent / "metrics_extracted.json"
    metrics_config_path = Path("pipeline/config/schemas/metrics/metrics_config.json")
    metrics_prompt_path = PROMPTS / "paper_prompts" / "metrics.md"
    metrics_schemas_dir = Path("pipeline/config/schemas/metrics")
    
    extracted_metrics = extract_metrics_for_papers(
        papers=task_classified_papers,
        metrics_config_path=metrics_config_path,
        metrics_prompt_path=metrics_prompt_path,
        schemas_dir=metrics_schemas_dir,
        use_gemini=gem,
        use_batch=use_batch,
        output_path=metrics_extracted_path
    )
    
    # Validate metrics
    click.echo("\nStep 3: Validating extracted metrics...")
    validated_metrics = validate_metrics(extracted_metrics)
    
    # Save validated metrics
    output_path = Path(output)
    write_json(output_path, validated_metrics)
    
    click.echo(f"\n✓ Extracted and validated {len(validated_metrics)} metric records")
    click.echo(f"✓ Results saved to {output_path}")
    
    # Summary by task type
    task_metric_counts = {}
    for record in validated_metrics:
        task_type = record.get('task_type', 'unknown')
        task_metric_counts[task_type] = task_metric_counts.get(task_type, 0) + 1
    
    if task_metric_counts:
        click.echo("\nMetrics by task type:")
        for task_type, count in sorted(task_metric_counts.items()):
            click.echo(f"  - {task_type}: {count} records")


@cli.command()
@click.option('--url', 'supabase_url', help='Supabase project URL (or set SUPABASE_URL env var)')
@click.option('--service-key', 'service_key', help='Supabase service role key (or set SUPABASE_SERVICE_ROLE_KEY env var)')
@click.option('--datasets-json', type=click.Path(exists=True), default='pipeline/data/dataset_data/datasets_seed_step2.json', show_default=True, help='Path to datasets JSON file')
@click.option('--papers-json', type=click.Path(exists=True), default='pipeline/data/paper_data/final_extracted.json', show_default=True, help='Path to papers JSON file')
@click.option('--metrics-json', type=click.Path(exists=False), default='pipeline/data/paper_data/metrics_final.json', show_default=True, help='Path to metrics JSON file')
@click.option('--matches-json', type=click.Path(exists=False), default='pipeline/data/dataset_data/paper_to_dataset_matches.jsonl', show_default=True, help='Path to paper-dataset matches JSONL file')
@click.option('--batch-size', type=int, default=500, show_default=True, help='Number of rows to upsert per batch')
@click.option('--dry-run', is_flag=True, default=False, help='Print what would be inserted without actually inserting')
@click.option('--init-db-url', 'init_db_url', default=None, help='Optional Postgres connection URL to initialize schema before push (runs supabase/migrations/0001_init.sql)')
@click.option('--log/--no-log', 'log', default=False, show_default=True, help='Enable verbose logging')
@click.option('--log-file', 'log_file', default=None, show_default=False, help='Optional path to write console output + logs. Defaults to pipeline/data/logs/<command>_<timestamp>.log')
def push(supabase_url, service_key, datasets_json, papers_json, metrics_json, matches_json, batch_size, dry_run, init_db_url, log, log_file):
    """Push pipeline data to Supabase database."""
    load_dotenv()
    configure_logging(verbose=log)
    log_path = Path(log_file) if log_file else Path(_default_log_file("push"))
    add_file_logging(str(log_path), verbose=log)
    click.echo(f"Logging to: {log_path}")
    with _tee_stdout_stderr(log_path):
        _run_push(supabase_url, service_key, datasets_json, papers_json, metrics_json, matches_json, batch_size, dry_run, init_db_url, log)


def _run_push(supabase_url, service_key, datasets_json, papers_json, metrics_json, matches_json, batch_size, dry_run, init_db_url, log):
    load_dotenv()
    configure_logging(verbose=log)
    
    # Get credentials from args or environment
    url = supabase_url or os.getenv('SUPABASE_URL')
    key = service_key or os.getenv('SUPABASE_SERVICE_ROLE_KEY') or os.getenv('SUPABASE_KEY')
    
    if not url:
        click.echo("Error: Supabase URL not provided. Use --url or set SUPABASE_URL environment variable.", err=True)
        return
    
    if not key:
        click.echo("Error: Supabase key not provided. Use --service-key or set SUPABASE_SERVICE_ROLE_KEY/SUPABASE_KEY environment variable.", err=True)
        return
    
    if dry_run:
        click.echo("=== DRY RUN MODE - No data will be inserted ===\n")
    
    click.echo(f"Pushing data to Supabase at {url}")
    click.echo(f"Datasets: {datasets_json}")
    click.echo(f"Papers: {papers_json}")
    if metrics_json:
        click.echo(f"Metrics: {metrics_json}")
    if matches_json:
        click.echo(f"Matches: {matches_json}")
    click.echo("")
    
    # Establish init DB URL priority: CLI > SUPABASE_DB_URL > derived from SUPABASE_URL + SUPABASE_PASSWORD
    derived_init_url = init_db_url
    if not dry_run and not derived_init_url:
        # 1) Direct env var override
        env_db_url = os.getenv('SUPABASE_DB_URL')
        if env_db_url:
            derived_init_url = env_db_url
        else:
            # 2) Derive from URL + password
            pwd = os.getenv('SUPABASE_PASSWORD')
            try:
                if pwd and url:
                    parsed = urlparse(url)
                    host = parsed.hostname or ""
                    project_ref = host.split(".")[0] if host else None
                    if project_ref:
                        db_host = f"db.{project_ref}.supabase.co"
                        derived_init_url = (
                            f"postgresql://postgres:{pwd}@{db_host}:5432/postgres?sslmode=require"
                        )
            except Exception:
                derived_init_url = None

    try:
        # Show a hint if we are going to initialize the schema
        if derived_init_url and log:
            click.echo(f"Initializing database schema using: {derived_init_url}")

        results = push_to_supabase(
            supabase_url=url,
            supabase_key=key,
            datasets_json=datasets_json if datasets_json else None,
            papers_json=papers_json if papers_json else None,
            metrics_json=metrics_json,
            batch_size=batch_size,
            dry_run=dry_run,
            init_db_url=derived_init_url if derived_init_url else None,
            paper_dataset_matches_path=matches_json if matches_json else None
        )
        
        click.echo("\n=== Push Summary ===")
        click.echo(f"✓ Datasets pushed: {results['datasets']}")
        click.echo(f"✓ Papers pushed: {results['papers']}")
        click.echo(f"✓ Metrics pushed: {results['metrics']}")
        if results.get('failed'):
            for table, n in results['failed'].items():
                click.echo(f"✗ {table}: {n} rows refused (see the log)", err=True)
            sys.exit(1)
        click.echo("\nPush completed successfully!")
        
    except Exception as e:
        # Provide a clearer hint if schema init fails with a network error
        if _unresolved_host(e):
            click.echo("\n✗ Database init failed: unable to resolve DB host.", err=True)
            click.echo("- Set SUPABASE_DB_URL in .env to your full Postgres URI (from Supabase → Settings → Database)", err=True)
            click.echo("- Or pass --init-db-url explicitly", err=True)
        click.echo(f"\n✗ Error during push: {e}", err=True)
        if log:
            import traceback
            traceback.print_exc()
        raise


@cli.command()
@click.option('--data', 'do_data', is_flag=True, default=False, help='Remove local pipeline data under pipeline/data/')
@click.option('--tables', 'do_tables', is_flag=True, default=False, help='Truncate Supabase tables (filtered_metrics, papers_dct, final_datasets)')
@click.option('--url', 'supabase_url', help='Supabase project URL (or set SUPABASE_URL env var) for DB URL derivation')
@click.option('--db-url', 'db_url', default=None, help='Full Postgres connection URL. Overrides --url and SUPABASE_DB_URL')
@click.option('--log/--no-log', 'log', default=False, show_default=True, help='Enable verbose logging')
@click.option('--log-file', 'log_file', default=None, show_default=False, help='Optional path to write console output + logs. Defaults to pipeline/data/logs/<command>_<timestamp>.log')
def cleanup(do_data, do_tables, supabase_url, db_url, log, log_file):
    load_dotenv()
    configure_logging(verbose=log)
    log_path = Path(log_file) if log_file else Path(_default_log_file("cleanup"))
    add_file_logging(str(log_path), verbose=log)
    click.echo(f"Logging to: {log_path}")
    with _tee_stdout_stderr(log_path):
        _run_cleanup(do_data, do_tables, supabase_url, db_url, log)


def _run_cleanup(do_data, do_tables, supabase_url, db_url, log):
    load_dotenv()
    configure_logging(verbose=log)

    if not do_data and not do_tables:
        do_data = True
        do_tables = True

    if do_data:
        DATA = Path("pipeline/data")
        PAPER_DATA = DATA / "paper_data"
        DATASET_DATA = DATA / "dataset_data"
        for p in [PAPER_DATA, DATASET_DATA]:
            if p.exists():
                click.echo(f"Removing directory: {p}")
                shutil.rmtree(p, onerror=_clear_readonly_and_retry)
        candidates = [
            DATA / "datasets.csv",
        ]
        for f in candidates:
            if f.exists():
                click.echo(f"Removing file: {f}")
                try:
                    f.unlink()
                except Exception:
                    pass
        click.echo("Local data cleanup complete.")

    if do_tables:
        derived_db_url = db_url
        if not derived_db_url:
            env_db_url = os.getenv('SUPABASE_DB_URL')
            if env_db_url:
                derived_db_url = env_db_url
            else:
                url = supabase_url or os.getenv('SUPABASE_URL')
                pwd = os.getenv('SUPABASE_PASSWORD')
                try:
                    if pwd and url:
                        parsed = urlparse(url)
                        host = parsed.hostname or ""
                        project_ref = host.split(".")[0] if host else None
                        if project_ref:
                            db_host = f"db.{project_ref}.supabase.co"
                            derived_db_url = (
                                f"postgresql://postgres:{pwd}@{db_host}:5432/postgres?sslmode=require"
                            )
                except Exception:
                    derived_db_url = None

        if not derived_db_url:
            click.echo("Error: Could not determine database URL. Provide --db-url, set SUPABASE_DB_URL, or set SUPABASE_URL and SUPABASE_PASSWORD.", err=True)
            return

        click.echo("Truncating Supabase tables: filtered_metrics, papers_dct, final_datasets ...")
        sql = "TRUNCATE TABLE filtered_metrics, papers_dct, final_datasets RESTART IDENTITY CASCADE;"
        try:
            with psycopg.connect(derived_db_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
                conn.commit()
            click.echo("Supabase tables truncated.")
        except Exception as e:
            if _unresolved_host(e):
                click.echo("\n✗ Database cleanup failed: unable to resolve DB host.", err=True)
                click.echo("- Set SUPABASE_DB_URL in .env to your full Postgres URI (from Supabase → Settings → Database)", err=True)
                click.echo("- Or pass --db-url explicitly", err=True)
            click.echo(f"\n✗ Error during DB cleanup: {e}", err=True)
            if log:
                import traceback
                traceback.print_exc()
            raise

if __name__ == '__main__':
    cli()
