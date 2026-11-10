import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional

import requests

from ..utils import get_logger
from pipeline.utils.llms.llm import log_event
from pipeline.utils.scraping.http import polite_get
from pipeline.src.provenance import archive_json, archive_text
from pipeline.src.utils import atomic_json

logger = get_logger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _serpapi_scholar(params: Dict[str, str], retries: int = 3, backoff_sec: float = 1.0) -> Dict:
    """Call SerpAPI Google Scholar engine with retries.

    Params should already include either 'q' or 'cites' and 'engine'.
    """
    api_key = os.getenv("SERPAPI_API_KEY")
    if not api_key:
        logger.warning("SERPAPI_API_KEY not set; search will return 0 results. Set it in your environment or .env")
    base_url = "https://serpapi.com/search"
    payload = {**params, "api_key": api_key or ""}

    for attempt in range(1, retries + 1):
        try:
            t0 = time.time()
            resp = polite_get(base_url, params=payload, timeout=30)
            log_event("serpapi", query=params.get("q") or f"cites:{params.get('cites')}", start=params.get("start"),
                      status=resp.status_code, seconds=round(time.time() - t0, 2),
                      # a refused request's body, archived (the page entry keeps only answers)
                      **({"error_sha256": archive_text("serpapi_error", resp.text or "")}
                         if resp.status_code != 200 else {}))
            if resp.status_code == 200:
                return resp.json()
            else:
                # Log status, params, and a short snippet of the response body for diagnostics (e.g., invalid key, quota)
                body_preview = ""
                try:
                    body_preview = (resp.text or "").strip()
                    if len(body_preview) > 300:
                        body_preview = body_preview[:300] + "..."
                except Exception:
                    body_preview = "<unavailable>"
                logger.warning(
                    "SerpAPI request failed (status=%s) attempt=%s params=%s body=%s",
                    resp.status_code,
                    attempt,
                    {k: v for k, v in payload.items() if k != "api_key"},
                    body_preview,
                )
        except requests.RequestException as e:
            logger.error("SerpAPI request exception on attempt %s: %s", attempt, e)
        time.sleep(backoff_sec * attempt)
    return {}


# One entry per query: what was asked, how many pages and results came back, and whether a
# page failed (a failed page returns nothing, which otherwise looks like "no results").
# run_all_searches / check_citations_of_top write it beside their output.
QUERY_LOG: List[Dict] = []
_LAST = threading.local()  # the calling thread's latest QUERY_LOG entry


def logged_searches(step: str, calls: List[Dict], search, threads: int = 1, desc: str = "",
                    fp: str = "") -> List[List[Dict]]:
    """search(call) for each call (a JSON-able dict, e.g. {"term": ...}), in `threads`
    threads and checkpointed per call (pipeline.src.checkpoint); a search with a failed
    page is retried. Returns each call's results, in order, and leaves QUERY_LOG holding
    the calls' entries in that order."""
    from pipeline.src.checkpoint import run_items, thread_runner

    def one(call):
        _LAST.entry = None
        results = search(call) or []
        return {"results": results, "log": getattr(_LAST, "entry", None) or {}}

    key = lambda call: json.dumps(call, sort_keys=True)  # noqa: E731
    done, failing = run_items(step, calls, key=key, fn=one, runner=thread_runner(threads, desc),
                              failed=lambda res: bool(res["log"].get("failed_pages")), fp=fp)
    last = {key(c): res for c, res in failing if isinstance(res, dict)}
    out = [done.get(key(c)) or last.get(key(c)) or {"results": [], "log": {}} for c in calls]
    QUERY_LOG.clear()
    QUERY_LOG.extend(o["log"] for o in out if o["log"])
    return [o["results"] for o in out]


def search_workers() -> int:
    """WSI_SEARCH_WORKERS (default 16): Scholar searches run in parallel (SerpAPI allows
    200k an hour), so 400 terms x 10 pages take minutes rather than hours."""
    try:
        return max(1, int(os.getenv("WSI_SEARCH_WORKERS", "16")))
    except ValueError:
        return 16


def write_query_log(out: str) -> None:
    path = Path(out).with_name(Path(out).stem + "_queries.json")
    atomic_json(path, QUERY_LOG, indent=1)


def _normalize_results(data: Dict) -> List[Dict]:
    items = []
    for r in data.get("organic_results", []) or []:
        title = r.get("title")
        link = r.get("link")
        snippet = r.get("snippet")
        year = None
        authors = None
        citations = None
        
        # Extract publication info
        pub = r.get("publication_info") or {}
        if isinstance(pub, dict):
            # Extract year
            year = pub.get("year") or pub.get("summary")
            
            # Extract authors from Google Scholar
            author_list = pub.get("authors", [])
            if author_list:
                author_names = []
                for author in author_list:
                    if isinstance(author, dict):
                        name = author.get("name", "")
                    elif isinstance(author, str):
                        name = author
                    else:
                        continue
                    if name:
                        author_names.append(name)
                if author_names:
                    authors = author_names
        
        # Extract citation info
        cited_by_id = None
        inline_links = r.get("inline_links") or {}
        cited = inline_links.get("cited_by") or {}
        if isinstance(cited, dict):
            cited_by_id = cited.get("cites_id")
            # Extract citation count
            total = cited.get("total")
            if total is not None:
                try:
                    citations = int(total)
                except (ValueError, TypeError):
                    pass
        
        # Extract scholar cluster ID
        scholar_id = r.get("result_id")
        
        item = {
            "title": title,
            "link": link,
            "snippet": snippet,
            "year": year,
            "authors": authors,
            "citations": citations,
            "cited_by_id": cited_by_id,
            "scholar_id": scholar_id,
            "source": "search",
        }
        
        # Store full scholar metadata for reference
        item["scholar_metadata"] = {
            "publication_info": pub,
            "inline_links": inline_links,
        }
        
        items.append(item)
    return items


def run_search(
    term: Optional[str] = None,
    cites_id: Optional[str] = None,
    *,
    per_page: int = 10,
    max_results_cap: Optional[int] = 100,
    max_pages: Optional[int] = None,
    page_sleep: float = 0.5,
) -> List[Dict]:
    """
    Run a Google Scholar search via SerpAPI for a query term or a citations-of query.

    This function paginates through all available result pages exposed by SerpAPI
    and aggregates them, effectively returning as many results as SerpAPI provides.
    """
    if not term and not cites_id:
        logger.error("run_search called without term or cites_id")
        return []

    base_params = {"engine": "google_scholar", "hl": "en"}
    if term:
        logger.info("Searching for term: %s", term)
        base_params["q"] = term
    else:
        logger.info("Searching for citations of cites_id: %s", cites_id)
        base_params["cites"] = cites_id

    # Pagination loop. Scholar supports `num` per page (1-20) and `start` offsets.
    # We'll continue while SerpAPI indicates there is a next page.
    start = 0
    aggregated: List[Dict] = []
    seen_keys = set()

    # Clamp parameters to valid ranges for the google_scholar engine
    if per_page < 1:
        per_page = 1
    if per_page > 20:
        per_page = 20
    if max_pages is not None and max_pages < 1:
        max_pages = 1
    if max_results_cap is not None and max_results_cap < 1:
        max_results_cap = None

    page_num = 0
    empty_streak = 0
    entry = {"provider": "serpapi:google_scholar", "query": term or "", "cites_id": cites_id or "",
             "started_at": _now(), "pages": [], "failed_pages": 0}
    QUERY_LOG.append(entry)
    _LAST.entry = entry
    while True:
        page_num += 1
        params = {**base_params, "start": start, "num": per_page}
        data = _serpapi_scholar(params)
        page_items = _normalize_results(data)
        # SerpAPI's answers as they came, archived by hash (pipeline.src.provenance): Scholar
        # gives a different page tomorrow. Every attempt, the empty retries too.
        raws = [archive_json("serpapi", data)] if data else []
        # Scholar answers "Google hasn't returned any results" for a page at random (the same
        # page gave 10 results, then 0, a moment apart); stopping there cut searches short.
        # An empty page is asked again, uncached, after a pause. SerpAPI doesn't bill these
        # empty answers, so retrying costs only time.
        waits = [float(w) for w in os.getenv("WSI_EMPTY_PAGE_WAITS", "2,5,10").split(",") if w.strip()]
        for wait in waits:
            if page_items or not data or "hasn't returned any results" not in str(data.get("error", "")):
                break
            time.sleep(wait)
            data = _serpapi_scholar({**params, "no_cache": "true"})
            page_items = _normalize_results(data)
            raws += [archive_json("serpapi", data)] if data else []
        retrieved_at = _now()
        info = (data or {}).get("search_information") or {}
        # An answer with an error (bad key, out of searches) is a failed page, not the end of
        # the results; Scholar's "no results" error is an empty page.
        error = str((data or {}).get("error") or "")
        ok = bool(data) and (not error or "hasn't returned any results" in error)

        entry["pages"].append({"page": page_num, "start": start, "items": len(page_items),
                               "retrieved_at": retrieved_at, "ok": ok,
                               **({"error": error[:300]} if error else {}),
                               "raw_sha256": raws[-1] if raws else "",
                               **({"attempt_sha256": raws} if len(raws) > 1 else {}),
                               "reported_total": info.get("total_results")})
        entry["failed_pages"] += not ok
        if not ok:
            entry["stopped"] = "failed_page"
            break
        for i, it in enumerate(page_items):
            it["provenance"] = {"provider": entry["provider"], "query": term or "", "cites_id": cites_id or "",
                                "page": page_num, "rank": start + i + 1, "retrieved_at": retrieved_at}
        # De-duplicate across pages using title+link as a simple key
        new_count = 0
        for it in page_items:
            key = f"{it.get('title')}|{it.get('link')}"
            if key not in seen_keys:
                seen_keys.add(key)
                aggregated.append(it)
                new_count += 1

        logger.info(
            "Search page %d (start=%d, num=%d) returned %d items (%d new, %d total)",
            page_num,
            start,
            per_page,
            len(page_items),
            new_count,
            len(aggregated),
        )

        # Determine if there is a next page from SerpAPI response
        pagination = (data or {}).get("serpapi_pagination") or {}
        has_next = bool(pagination.get("next"))

        # Stop if we've reached the requested max results cap
        if max_results_cap is not None and len(aggregated) >= max_results_cap:
            logger.info(
                "Stopping search due to max results cap: %d (current total=%d)",
                max_results_cap,
                len(aggregated),
            )
            entry["stopped"] = "results_cap"
            break

        # Stop conditions: no next page, a page of only repeats, max_pages, or two empty pages
        # in a row (one empty page, even after the retries, can still be followed by results).
        empty_streak = empty_streak + 1 if not page_items else 0
        stop = ("no_next_page" if not has_next else "only_repeats" if page_items and new_count == 0
                else "empty_pages" if empty_streak >= 2
                else "max_pages" if max_pages is not None and page_num >= max_pages else "")
        if stop:
            entry["stopped"] = stop
            break

        # Increment by the requested page size
        start += per_page
        time.sleep(page_sleep)

    entry["results"] = len(aggregated)
    logger.info("Search returned %d total aggregated results", len(aggregated))
    return aggregated

def run_all_searches(
    terms: List[str],
    out: str,
    *,
    search_func=run_search,
    **search_kwargs,
) -> List[Dict]:
    """
    Runs a search for each term in the list and saves the results to a JSON file.

    Args:
        terms: A list of search terms.
        out: The path to the output JSON file.
        search_func: The function to use for running a search.

    Returns:
        A list of all paper records found.
    """
    all_results: List[Dict] = []
    from pipeline.src.checkpoint import fingerprint
    per_term = logged_searches("1.1_search", [{"term": t} for t in terms],
                               lambda call: search_func(call["term"], **search_kwargs),
                               threads=search_workers(), desc="Search terms", fp=fingerprint(search_kwargs))
    for term, results in zip(terms, per_term):
        logger.info("Term results: '%s' -> %d", term, len(results))
        all_results.extend(results)

    # Persist aggregated results
    atomic_json(out, all_results, indent=4)
    write_query_log(out)

    logger.info("Completed run_all_searches: %d terms, %d total results", len(terms), len(all_results))
    return all_results
