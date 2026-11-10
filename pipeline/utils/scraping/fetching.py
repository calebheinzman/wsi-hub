from io import BytesIO
from bs4 import BeautifulSoup
try:  # pragma: no cover - optional dependency
    from PyPDF2 import PdfReader
    from PyPDF2.errors import PdfReadError, DependencyError
except ModuleNotFoundError:  # pragma: no cover - optional dependency absent
    PdfReader = None

    class PdfReadError(Exception):
        """Fallback PdfReadError when PyPDF2 is not installed."""


    class DependencyError(Exception):
        """Fallback DependencyError when PyPDF2 is not installed."""

from pipeline.utils.scraping.browser_fetcher import fetch_with_browser
from pipeline.utils.scraping.http import HostCooling, polite_get


def pdf_text(data: bytes) -> str:
    """The text of a PDF given as bytes ("" if it doesn't parse)."""
    if not data or PdfReader is None:
        return ""
    try:
        return "".join(page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages)
    except Exception:  # noqa: BLE001 - PyPDF2 raises many kinds on a damaged file
        return ""


def extract_text_from_url(url, use_browser=False):
    """
    Download and extract text from an HTML or PDF URL.
    
    Args:
        url: The URL to fetch.
        use_browser: If True, retry failed fetches with headless browser
        
    Returns:
        Extracted text, or empty string if failed.
    """
    try:
        resp = polite_get(url, timeout=30)
        content_type = resp.headers.get('Content-Type', '').split(';')[0]
        # A block page (CAPTCHA, bot check) comes back 200; its text is not the paper.
        if resp.status_code != 200 or getattr(resp, "blocked", False):
            if use_browser:
                print(f"[extract_text_from_url] Status {resp.status_code}, retrying with browser for {url}")
                text = fetch_with_browser(url, timeout=30)
                return text or ""
            return ""
        if 'pdf' in content_type and PdfReader is not None:
            # Retry logic for PDF extraction
            for attempt in range(3):
                try:
                    file = BytesIO(resp.content)
                    pdf = PdfReader(file)
                    text = ''
                    for page in pdf.pages:
                        t = page.extract_text()
                        if t:
                            text += t
                    if text.strip():
                        return text
                    # Empty text extracted
                    if attempt == 2:
                        print(f"[extract_text_from_url] PDF extracted but no text found for {url}")
                        if use_browser:
                            print(f"[extract_text_from_url] Retrying empty PDF with browser for {url}")
                            text = fetch_with_browser(url, timeout=30)
                            return text or ""
                        return ""
                except (PdfReadError, DependencyError) as e:
                    print(f"[extract_text_from_url] PDF error on attempt {attempt+1} for {url}: {e}")
                    if attempt == 2:
                        if use_browser:
                            print(f"[extract_text_from_url] Retrying failed PDF with browser for {url}")
                            text = fetch_with_browser(url, timeout=30)
                            return text or ""
                        return ""
            return ""
        elif 'pdf' in content_type:
            print("[extract_text_from_url] PyPDF2 not available, cannot parse PDF content")
            if use_browser:
                print(f"[extract_text_from_url] Retrying PDF (no PyPDF2) with browser for {url}")
                text = fetch_with_browser(url, timeout=30)
                return text or ""
            return ""
        elif 'html' in content_type:
            soup = BeautifulSoup(resp.content, 'html.parser')
            return soup.get_text(separator=' ', strip=True)
        else:
            if use_browser:
                print(f"[extract_text_from_url] Unknown content type, trying browser for {url}")
                text = fetch_with_browser(url, timeout=30)
                return text or ""
            return ""
    except HostCooling:
        return ""  # the site is refusing us for now; the caller's fallbacks take over
    except Exception as e:
        print(f"[extract_text_from_url] Failed for {url}: {e}")
        if use_browser:
            print(f"[extract_text_from_url] Retrying with browser for {url}")
            text = fetch_with_browser(url, timeout=30)
            return text or ""
        return ""
