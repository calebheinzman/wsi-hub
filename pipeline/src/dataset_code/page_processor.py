"""Module for fetching and processing web pages for dataset information."""
import os
import json
import logging
import time
import random
from pathlib import Path
from typing import List, Dict, Optional
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from pipeline.utils.llms.llm import run_gpt, strip_code_fence
from pipeline.utils.scraping.browser_fetcher import fetch_multiple_with_browser
from pipeline.utils.scraping.fetching import extract_text_from_url
from pipeline.utils.scraping.http import polite_get

from pipeline.src.utils import atomic_json

logger = logging.getLogger(__name__)


BLOCKED_DOMAINS = {
    "www.sciencedirect.com",
    "sciencedirect.com",
    "academic.oup.com",
    "www.researchgate.net",
    "researchgate.net",
}


def _jina_reader_url(url: str) -> str:
    # Jina AI reader returns readable text without JS; good fallback for blocked pages
    # It expects http(s):// prefix retained
    return f"https://r.jina.ai/{url}"


def fetch_page_content(url: str, timeout: int = 30) -> Optional[str]:
    """
    Fetch and extract text content from a URL.
    
    Args:
        url: URL to fetch
        timeout: Request timeout in seconds
        
    Returns:
        Extracted text content or None if failed
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (compatible; WSI-Hub-Bot/1.0; +https://github.com/wsi-hub)'
        }
        response = None
        max_retry_after_s = float(os.getenv("DATASET_FETCH_MAX_RETRY_AFTER_S", "60"))
        for attempt in range(3):
            response = polite_get(url, timeout=timeout, headers=headers)

            if response.status_code == 200 and getattr(response, "blocked", False):
                raise requests.exceptions.HTTPError("block page")  # a bot check, not the page
            if response.status_code == 200:
                break

            if response.status_code in {429, 500, 502, 503, 504}:
                retry_after = (response.headers.get('Retry-After') or '').strip()
                wait_s = None
                if retry_after.isdigit():
                    wait_s = min(float(retry_after), max_retry_after_s)
                if wait_s is None:
                    wait_s = min(2.0 * (2 ** attempt), 10.0) + random.random() * 0.25
                logger.warning(
                    f"Failed to fetch {url}: status {response.status_code} (attempt {attempt + 1}/3); "
                    f"retrying after {wait_s:.1f}s"
                )
                time.sleep(wait_s)
                continue

            logger.warning(f"Failed to fetch {url}: status {response.status_code}")
            raise requests.exceptions.HTTPError(f"status {response.status_code}")

        if response is None or response.status_code != 200:
            status = response.status_code if response is not None else 'no_response'
            logger.warning(f"Failed to fetch {url}: status {status}")
            raise requests.exceptions.HTTPError(f"status {status}")
        
        content_type = response.headers.get('Content-Type', '').lower()
        
        if 'html' in content_type:
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Remove script and style elements
            for script in soup(['script', 'style', 'nav', 'footer', 'header']):
                script.decompose()
            
            # Get text
            text = soup.get_text(separator=' ', strip=True)
            
            # Clean up whitespace
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            text = ' '.join(chunk for chunk in chunks if chunk)
            
            return text
        elif 'text/plain' in content_type and urlparse(url).netloc.startswith('r.jina.ai'):
            # Jina reader returns text/plain body with extracted content
            return response.text.strip()
        elif 'pdf' in content_type:
            # Use shared extractor that can handle PDFs and HTML (used elsewhere in the pipeline)
            text = (extract_text_from_url(url, use_browser=False) or "").strip()
            if text:
                return text
            # Don't log warning here - will try fallbacks
            raise ValueError(f"PDF extraction failed for {url}")
        else:
            logger.warning(f"Unsupported content type for {url}: {content_type}")
            raise ValueError(f"unsupported content type: {content_type}")
            
    except Exception as e:
        # Fallback: try Jina AI reader which often bypasses client-side blocks
        try:
            return fetch_via_jina(url, timeout=timeout)
        except Exception as ee:
            logger.warning(f"Fallback failed for {url}: {ee}")
            return None


def fetch_via_jina(url: str, timeout: int = 30, max_retries: int = 3) -> Optional[str]:
    """Fetch content using Jina reader with retry logic for rate limits. polite_get keeps
    Jina calls 1.5 s apart across every process of the run."""
    jurl = _jina_reader_url(url) if 'r.jina.ai' not in url else url
    
    for attempt in range(max_retries):
        try:
            r2 = polite_get(jurl, timeout=timeout)
            
            if r2.status_code == 200 and r2.headers.get('Content-Type','').lower().startswith('text/'):
                text = r2.text.strip()
                return text if text else None
            
            if r2.status_code == 429:
                # Rate limited - exponential backoff
                wait_time = (2 ** attempt) + random.random()
                logger.debug(f"Jina rate limited for {url}, waiting {wait_time:.1f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                continue
            
            # Other non-200 status - don't retry
            logger.warning(f"Jina reader fallback failed for {url}: status {r2.status_code}")
            return None
            
        except requests.exceptions.Timeout:
            logger.warning(f"Jina reader timeout for {url} (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                time.sleep(1 + random.random())
                continue
            return None
        except Exception as e:
            logger.warning(f"Jina reader error for {url}: {e}")
            return None
    
    logger.warning(f"Jina reader failed for {url} after {max_retries} retries")
    return None


def get_url_slug(url: str, rank: int, windows: bool = os.name == "nt") -> str:
    """
    Generate a filename-safe slug from URL and rank.
    
    Args:
        url: URL to slugify
        rank: Search result rank
        
    Returns:
        Slug string like "0_github_com"
    """
    parsed = urlparse(url)
    host = parsed.netloc.replace('www.', '').replace('.', '_')
    if windows:
        # "host:8080" in a file name is an NTFS alternate data stream: the page would vanish
        host = host.replace(':', '_')
    return f"{rank}_{host}"


PAGES_INDEX = "pages_index.jsonl"


def keep_page(output_path: Path, url: str, via: str) -> None:
    """Archive a saved page whole (pipeline.src.provenance) and add it to the dataset's
    pages_index.jsonl: url, file, sha256, time. The page files are a cache that a changed
    search replaces, and only the first 8,000 characters went to the summarizer."""
    from datetime import datetime, timezone
    from pipeline.src.provenance import archive_text
    from pipeline.utils.llms.llm import _append
    text = output_path.read_text(encoding="utf-8", errors="replace")
    _append(str(output_path.parent.parent / PAGES_INDEX), json.dumps({
        "url": url, "file": output_path.name, "sha256": archive_text("dataset_page", text), "via": via,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}) + "\n")


def page_url(path: Path) -> str:
    """The URL a saved page came from (its first line)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            first = f.readline()
    except OSError:
        return ""
    return first[5:].strip() if first.startswith("URL: ") else ""


def fetch_and_save_pages(
    search_results: List[Dict[str, str]],
    output_dir: Path,
    max_pages: int = 10,
    use_browser: bool = False
) -> List[Path]:
    """
    Fetch and save page content from search results.
    
    Args:
        search_results: List of dicts with 'link' key
        output_dir: Directory to save pages
        max_pages: Maximum number of pages to fetch
        use_browser: If True, retry failed fetches with headless browser
        
    Returns:
        List of paths to saved page files
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    saved_paths = []
    failed_fetches = []  # Track failed fetches for browser retry
    
    # Prefer non-paywalled domains first by stable ordering
    def score_result(u: str) -> int:
        d = urlparse(u).netloc.lower()
        # Lower score is better
        preferred = (
            'github.com', 'huggingface.co', 'grand-challenge.org', 'kaggle.com',
            'zenodo.org', 'arxiv.org', 'readthedocs.io', 'ni-hub.org', 'uni-',
        )
        if any(p in d for p in preferred):
            return 0
        if d in BLOCKED_DOMAINS:
            return 5
        return 2

    ordered = sorted(
        [r for r in search_results if r.get('link')],
        key=lambda r: score_result(r.get('link',''))
    )

    for rank, result in enumerate(ordered[:max_pages]):
        url = result.get('link', '')
        if not url:
            continue
        
        slug = get_url_slug(url, rank)
        output_path = output_dir / f"{slug}.txt"
        
        # Skip if already exists
        if output_path.exists():
            logger.debug(f"Page already fetched: {output_path}")
            # A page saved by a session that stopped before archiving it, or whose archive
            # write failed: archive it now (a page already archived is not written twice).
            keep_page(output_path, url, "cached")
            saved_paths.append(output_path)
            continue
        
        # Skip known blocked/p paywalled domains (will still try Jina fallback during fetch)
        netloc = urlparse(url).netloc.lower()

        # Fetch content
        logger.info(f"Fetching page {rank}: {url}")
        content = None
        if netloc in BLOCKED_DOMAINS:
            content = fetch_via_jina(url)
        else:
            # fetch_page_content already attempts Jina fallback internally on failures
            content = fetch_page_content(url)
        
        if content:
            # Save raw text
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(f"URL: {url}\n")
                f.write(f"Title: {result.get('title', '')}\n")
                f.write(f"Snippet: {result.get('snippet', '')}\n")
                f.write("\n" + "="*80 + "\n\n")
                f.write(content)
            keep_page(output_path, url, "fetch")

            saved_paths.append(output_path)
            logger.info(f"Saved page content to {output_path}")
        else:
            logger.warning(f"Failed to fetch content from {url}")
            # Track for browser retry
            failed_fetches.append((url, output_path, result))
        
        # Rate limiting
        time.sleep(0.5 + random.random() * 0.5)
    
    # Browser fallback for failed fetches
    if use_browser and failed_fetches:
        logger.info(f"Retrying {len(failed_fetches)} failed fetches with browser...")
        
        # Prepare batch for browser fetching
        browser_batch = [(url, str(output_path)) for url, output_path, _ in failed_fetches]
        
        # Fetch with browser
        browser_results = fetch_multiple_with_browser(browser_batch, timeout=30)
        
        # Save successful browser fetches
        for url, output_path, result in failed_fetches:
            content = browser_results.get(str(output_path))
            if content:
                output_path_obj = Path(output_path)
                with open(output_path_obj, 'w', encoding='utf-8') as f:
                    f.write(f"URL: {url}\n")
                    f.write(f"Title: {result.get('title', '')}\n")
                    f.write(f"Snippet: {result.get('snippet', '')}\n")
                    f.write(f"Fetched: browser fallback\n")
                    f.write("\n" + "="*80 + "\n\n")
                    f.write(content)
                keep_page(output_path_obj, url, "browser")

                saved_paths.append(output_path_obj)
                logger.info(f"Browser fallback succeeded for {url}")
            else:
                logger.warning(f"Browser fallback also failed for {url}")
        
        logger.info(f"Browser fallback recovered {sum(1 for r in browser_results.values() if r)} / {len(failed_fetches)} failed fetches")
    
    return saved_paths


def summarize_page(
    page_path: Path,
    entity_name: str,
    prompt_path: Path,
    use_gemini: bool = False,
    entity_type: str = "dataset"
) -> Dict:
    """
    Summarize a page's content focusing on dataset or model information.
    
    Args:
        page_path: Path to page text file
        entity_name: Name of dataset or model to focus on
        prompt_path: Path to sum_page.md prompt
        use_gemini: Whether to use Gemini
        entity_type: Type of entity ('dataset' or 'model')
        
    Returns:
        Dict with summary and metadata
    """
    summary_path = page_path.with_suffix('.summary.json')
    
    # Check if already summarized
    if summary_path.exists():
        logger.debug(f"Summary already exists: {summary_path}")
        with open(summary_path, 'r', encoding="utf-8") as f:
            return json.load(f)
    
    # Read page content
    with open(page_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Limit content length to avoid token limits
    max_chars = 8000
    if len(content) > max_chars:
        content = content[:max_chars] + "\n\n[Content truncated...]"
    
    # Read prompt
    with open(prompt_path, 'r', encoding="utf-8") as f:
        prompt = f.read()
    
    # Build user prompt
    entity_label = "Dataset Name" if entity_type == "dataset" else "Model Name"
    user_prompt = f"{entity_label}: {entity_name}\n\nPage Content:\n{content}"
    
    try:
        response = run_gpt(
            system_prompt=prompt,
            user_prompt=user_prompt,
            model="gemini-3-flash-preview" if use_gemini else os.getenv("WSI_DATASET_MODEL", "gpt-6-luna"),
            use_gemini=use_gemini
        )
        
        summary_text = response.choices[0].message.content or ""
        # sum_page asks for a bare string, but models fence it anyway --
        # validate_run.py found fenced summaries in the January corpus.
        summary_text = strip_code_fence(summary_text)
        
        # Create summary object
        entity_key = "dataset_name" if entity_type == "dataset" else "model_name"
        summary = {
            entity_key: entity_name,
            "page_path": Path(page_path).as_posix(),
            "summary": summary_text,
            "has_content": bool(summary_text.strip()),
            "content_length": len(summary_text)
        }
        
        # Save summary
        atomic_json(summary_path, summary, indent=2)
        
        logger.info(f"Saved summary to {summary_path}")
        return summary
        
    except Exception as e:
        logger.error(f"Error summarizing page {page_path}: {e}")
        entity_key = "dataset_name" if entity_type == "dataset" else "model_name"
        return {
            entity_key: entity_name,
            "page_path": Path(page_path).as_posix(),
            "summary": "",
            "has_content": False,
            "error": str(e)
        }


def summarize_all_pages(
    page_paths: List[Path],
    entity_name: str,
    prompt_path: Path,
    use_gemini: bool = False,
    entity_type: str = "dataset"
) -> List[Dict]:
    """
    Summarize all pages for a dataset or model.
    
    Args:
        page_paths: List of paths to page files
        entity_name: Name of dataset or model
        prompt_path: Path to sum_page.md prompt
        use_gemini: Whether to use Gemini
        entity_type: Type of entity ('dataset' or 'model')
        
    Returns:
        List of summary dicts
    """
    summaries = []
    
    for page_path in page_paths:
        summary = summarize_page(page_path, entity_name, prompt_path, use_gemini, entity_type)
        if summary.get('has_content'):
            summaries.append(summary)
    
    logger.info(f"Generated {len(summaries)} summaries with content for {entity_name}")
    return summaries
