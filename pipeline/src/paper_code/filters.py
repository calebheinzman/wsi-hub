import json
import logging
import os
import random
import re
import threading
import time
from pathlib import Path
from typing import List, Tuple

from tqdm import tqdm

from pipeline.utils.llms.llm import log_event, run_batch_requests, run_gpt, _extract_batch_content
from pipeline.utils.scraping import lookup_cache
from pipeline.utils.scraping.fetching import extract_text_from_url, pdf_text
from pipeline.utils.scraping.http import contact_email, host_state, interleave_by_host, polite_get, with_host_state
from pipeline.src.utils import atomic_json, replace_retry


logger = logging.getLogger(__name__)

CRITERIA_PATH = Path("pipeline/config/supp/criteria.txt")


def _load_criteria() -> Tuple[List[str], List[str]]:
    """
    Load criteria.txt and split into (positives, negatives).
    Everything before the first blank line is considered positive criteria,
    everything after is negative criteria. Each non-empty line is one criterion.
    """
    try:
        lines = CRITERIA_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return [], []
    pos: List[str] = []
    neg: List[str] = []
    in_neg = False
    for ln in lines:
        if ln.strip() == "":
            in_neg = True
            continue
        (neg if in_neg else pos).append(ln.strip())
    return pos, neg


def _structured_prompt_path(prompt_path: str) -> Path:
    """The full-text gate prompt, in the same directory as `prompt_path`."""
    return Path(prompt_path).parent / "check_all_criteria_structured.md"


def _render_structured(template: str, pos_lines: List[str], neg_lines: List[str]) -> str:
    pos_block = "\n".join(f"{i}. {c}" for i, c in enumerate(pos_lines))
    neg_block = "\n".join(f"{i}. {c}" for i, c in enumerate(neg_lines))
    return template.replace("{pos_block}", pos_block).replace("{neg_block}", neg_block)


def _decide_structured_gate(content: str | None, n_pos: int, n_neg: int) -> bool | None:
    """Apply the keep/drop rule to one JSON answer.

    Keep iff every positive criterion is true AND no negative criterion is true.
    Returns None when the answer cannot be parsed (caller treats as reject).
    """
    if not content:
        return None
    try:
        # Parse directly rather than via is_valid_json_response: that helper's
        # recovery path rewrites numeric-string keys ("0" -> ""), which would
        # corrupt the per-criterion map. strip_json_wrapper only removes fences.
        from pipeline.utils.llms.llm import strip_json_wrapper
        parsed = json.loads(strip_json_wrapper(content))
        if not isinstance(parsed, dict):
            return None
        positives = parsed.get("positives", {}) or {}
        negatives = parsed.get("negatives", {}) or {}

        def _truthy(v) -> bool:
            if isinstance(v, bool):
                return v
            return str(v).strip().lower() in ("true", "1", "yes")

        # All positives must be present and true.
        for i in range(n_pos):
            if not _truthy(positives.get(str(i), positives.get(i))):
                return False
        # No negative may be true.
        for i in range(n_neg):
            if _truthy(negatives.get(str(i), negatives.get(i))):
                return False
        return True
    except Exception:
        return None


MIN_FULLTEXT_CHARS = int(os.getenv("WSI_MIN_FULLTEXT_CHARS", "2000"))


# --- Scholarly lookups ---------------------------------------------------------------
# OpenAlex prices requests in credits: a lookup by identifier (DOI, PMC id, PubMed id) is
# free, a title search costs 10, and a key gets 10,000 a day -- about 1,000 title searches.
# Scholar results carry no DOI, so every lookup used to be a title search, made up to three
# times per paper; the day's credits were gone after ~1,000 papers, and every later paper
# silently lost its open-access copy and abstract. Now a paper is looked up by identifier
# first -- the DOI its URL carries, else the one Crossref (free) has for its title -- and
# by title search only while credits last. Every answer is cached (lookup_cache).
# OpenAlex also keeps its own copy of ~60M papers' PDFs (content.openalex.org, 100 credits a
# download): kept papers we could only read the abstract of are read from that copy.

_DOI_IN_URL = re.compile(r"\b(10\.\d{4,9}/[^\s?#&\"<>]+)", re.IGNORECASE)
# What publisher URLs put after the DOI: /full, /abstract, /pdf, .full.pdf, a bioRxiv version.
_DOI_TAIL = re.compile(r"(?:/(?:full|abstract|meta|pdf|epdf|html|fulltext|summary|references|figures|"
                       r"full-text|download)|\.(?:full|abstract|pdf|full-text|short)|[.,;)\]])$", re.IGNORECASE)


def doi_from_url(url: str) -> str:
    """The DOI a paper URL carries, or "": /doi/10.1002/..., /article/10.1007/..., ?id=10.1371/...,
    doi.org/..., Nature /articles/s41...-..., and arXiv ids (DataCite 10.48550)."""
    from urllib.parse import unquote
    u = unquote(url or "")
    m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", u, re.IGNORECASE)
    if m:
        return f"10.48550/arXiv.{m.group(1)}"
    m = re.search(r"nature\.com/articles/(s\d{5}-\d{3}-\d{4,5}-[0-9a-z])", u, re.IGNORECASE)
    if m:
        return f"10.1038/{m.group(1)}"
    m = _DOI_IN_URL.search(u)
    if not m:
        return ""
    doi = m.group(1)
    while True:
        trimmed = _DOI_TAIL.sub("", doi)
        if trimmed.startswith("10.1101/"):
            trimmed = re.sub(r"v\d+$", "", trimmed)
        if trimmed == doi:
            return doi
        doi = trimmed


def _norm_title(t: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (t or "").lower()).split())


def _same_title(found: str, wanted: str) -> bool:
    """A confident title match (Scholar truncates titles, so a 60-character prefix will do):
    a wrong paper's text is worse than none."""
    a, b = _norm_title(found), _norm_title(wanted)
    return bool(a and b and (a.startswith(b[:60]) or b.startswith(a[:60])))


def _close_title(found: str, wanted: str) -> bool:
    """Looser, for a record reached by the paper's own identifier: the same title, or most of
    its words (Scholar's titles carry "[PDF]" tags and lose punctuation)."""
    if _same_title(found, wanted):
        return True
    a, b = set(_norm_title(found).split()), set(_norm_title(wanted).split())
    return bool(a and b) and len(a & b) / len(a | b) >= 0.5


def crossref_doi(title: str):
    """The DOI of the Crossref record with this title: "" when there is none, None when
    Crossref could not be asked (asked again next time). Free, 1 request a second."""
    t = (title or "").lstrip("… ").strip()
    if len(_norm_title(t)) < 20:
        return ""  # too generic to match on

    def ask():
        params = {"query.bibliographic": t, "rows": 3, "select": "DOI,title"}
        if contact_email():
            params["mailto"] = contact_email()
        try:
            r = polite_get("https://api.crossref.org/works", params=params, timeout=20)
        except Exception:  # noqa: BLE001 - a fallback must never fail the paper
            return None
        if r.status_code != 200:
            return None
        for rec in (r.json().get("message") or {}).get("items") or []:
            if any(_same_title(x, t) for x in rec.get("title") or []):
                return rec.get("DOI") or ""
        return ""

    return lookup_cache.cached("crossref_doi", _norm_title(t), ask, keep=lambda v: v is not None)


_OPENALEX = "api.openalex.org"
_budget_warned = False


def _openalex_search_allowed() -> bool:
    """Title searches cost credits: off with WSI_OPENALEX_SEARCH=0, and skipped (by every
    process) once the key's daily credits are spent, until they reset."""
    if os.getenv("WSI_OPENALEX_SEARCH", "1").lower() in ("0", "false", "no"):
        return False
    return float(host_state(_OPENALEX).get("search_blocked_until", 0)) <= time.time()


_SEARCH_CREDITS, _CONTENT_CREDITS = 10, 100


def _credits_left(r):
    """Credits the key has left (None when the answer doesn't say): the day's free ones,
    then one-time credits, then the prepaid balance (10,000 credits a dollar)."""
    remaining = (r.headers.get("X-RateLimit-Remaining") or "").strip()
    if not remaining.isdigit():
        return None
    try:
        onetime = float(r.headers.get("X-RateLimit-Onetime-Remaining") or 0)
        prepaid = float(r.headers.get("X-RateLimit-Prepaid-Remaining-USD") or 0)
    except ValueError:
        onetime = prepaid = 0.0
    return int(remaining) + onetime + prepaid * 10_000


def _note_openalex_budget(r) -> None:
    global _budget_warned
    # The budget, not a per-second 429: the credits headers say what is left (a 429
    # without them is a momentary limit, and a paper looked up then is asked again).
    left = _credits_left(r)
    if left is None or left >= _CONTENT_CREDITS:
        return
    remaining = r.headers.get("X-RateLimit-Remaining")
    reset = (r.headers.get("X-RateLimit-Reset") or r.headers.get("Retry-After") or "").strip()
    try:
        reset_s = float(reset)
    except ValueError:
        reset_s = 3600.0
    if reset_s > 1e9:  # an epoch time rather than seconds from now
        reset_s -= time.time()
    until = time.time() + min(max(reset_s, 60.0), 86400.0)  # never more than a day
    # Too few for a download; title searches (10 credits) go on until they're too few too.
    blocked = {"content_blocked_until": until}
    if left < _SEARCH_CREDITS:
        blocked["search_blocked_until"] = until
    with_host_state(_OPENALEX, lambda s: s.update(blocked))
    if left >= _SEARCH_CREDITS:
        return
    log_event("openalex_budget", remaining=remaining, reset_s=reset)
    if not _budget_warned:
        _budget_warned = True
        logger.warning("OpenAlex credits are spent%s: title searches and PDF downloads are off for %.1f h; "
                       "lookups by DOI (free) continue.", "" if os.getenv("OPENALEX_API_KEY") else " (no OPENALEX_API_KEY set)",
                       (until - time.time()) / 3600)


def _openalex_get(url: str, params: dict):
    """GET from OpenAlex with the user's key when set (OPENALEX_API_KEY). None on a failed
    request; a 429 is returned (and, when it is the daily budget, recorded)."""
    headers = {"Authorization": f"Bearer {os.environ['OPENALEX_API_KEY']}"} if os.getenv("OPENALEX_API_KEY") else {}
    try:
        r = polite_get(url, params=params, headers=headers, timeout=20)
    except Exception as exc:  # noqa: BLE001 - a fallback must never fail the paper
        logger.debug("OpenAlex request failed for %s: %s", url, exc)
        return None
    _note_openalex_budget(r)
    return r


def _openalex_ids(item: dict) -> list:
    """Identifiers OpenAlex resolves for free: the DOI (the record's, or its URL's), PMC, PubMed."""
    link = item.get("link") or item.get("url") or ""
    doi = (item.get("doi") or "").strip() or doi_from_url(link) or doi_from_url(item.get("pdf_url") or "")
    ids = [f"doi:{doi}"] if doi else []
    m = re.search(r"PMC\d+", link)
    if m:
        ids.append(f"pmcid:{m.group()}")
    m = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", link)
    if m:
        ids.append(f"pmid:{m.group(1)}")
    return ids


def _find_openalex_work(item: dict) -> tuple:
    """(record or {}, whether the answer is definite enough to cache)."""
    params = {"mailto": contact_email()} if contact_email() else {}
    title = (item.get("title") or "").lstrip("… ").strip()
    definite = True
    ids = _openalex_ids(item)
    if not ids and title:
        doi = crossref_doi(title)
        if doi is None:
            definite = False
        elif doi:
            ids = [f"doi:{doi}"]
    for ident in ids:
        r = _openalex_get(f"https://api.openalex.org/works/{ident}", params)
        if r is not None and r.status_code == 200:
            w = r.json()
            if not title or _close_title(w.get("title"), title):
                return w, True
        elif r is None or r.status_code != 404:
            definite = False
    if not title:
        return {}, definite
    if not _openalex_search_allowed():
        return {}, False
    r = _openalex_get("https://api.openalex.org/works", {**params, "search": title, "per-page": 1})
    if r is None or r.status_code != 200:
        return {}, False
    w = (r.json().get("results") or [{}])[0]
    return (w if _same_title(w.get("title"), title) else {}), definite


def _openalex_work(item: dict) -> dict:
    """The OpenAlex record for a paper ({} if none): by identifier (free), else a title
    search while the key's credits last. Cached per paper across processes and runs."""
    key = ((item.get("doi") or "").lower() or item.get("link") or item.get("url")
           or _norm_title(item.get("title") or ""))
    hit = lookup_cache.get("openalex_work", key)
    if hit is not lookup_cache.MISS:
        return hit
    try:
        work, definite = _find_openalex_work(item)
    except Exception as exc:  # noqa: BLE001 - a fallback must never fail the paper
        logger.debug("OpenAlex lookup failed for %s: %s", item.get("title", ""), exc)
        return {}
    if definite:
        lookup_cache.put("openalex_work", key, work)
    return work


def open_access_text(item: dict, use_browser: bool = False) -> str:
    """Text for a paper whose page could not be scraped (or gave only a stub).

    Tries Europe PMC for a PMC article, then the open-access copies OpenAlex and
    Semantic Scholar know (publisher PDF, PMC, repositories, arXiv), then falls back to
    title + abstract (OpenAlex's, else Semantic Scholar's), marked as abstract-only. On the human
    relevance gold, 9 of 11 papers without usable scraped text had an OpenAlex
    abstract and 8 an open-access copy; without this they were dropped unjudged.
    """
    text = _europe_pmc_text(item.get("link") or "")
    if text:
        return text
    w = _openalex_work(item)
    locs = [w.get("best_oa_location")] + list(w.get("oa_locations") or []) if w else []
    urls = [(loc or {}).get("pdf_url") or (loc or {}).get("landing_page_url") for loc in locs]
    # Then Semantic Scholar's copies, asked only when OpenAlex's gave nothing: its key
    # allows ~1 request a second for the whole run (and answers 429 erratically even then).
    tried = set()
    for source in (lambda: urls, lambda: _semantic_scholar_pdf_urls(item)):
        for url in [u for u in dict.fromkeys(source()) if u and u not in tried]:
            tried.add(url)
            try:
                text = (extract_text_from_url(url, use_browser=use_browser) or "").strip()
            except Exception:  # noqa: BLE001
                text = ""
            if len(text) >= MIN_FULLTEXT_CHARS:
                return text
    inv = (w or {}).get("abstract_inverted_index") or {}
    if inv:
        words = sorted((pos, word) for word, positions in inv.items() for pos in positions)
        abstract = " ".join(word for _, word in words)
        return f"[abstract only]\nTitle: {w.get('title') or item.get('title', '')}\n\nAbstract: {abstract}"
    abstract = (_semantic_scholar_hit(item).get("abstract") or "").strip()
    if abstract:
        return f"[abstract only]\nTitle: {item.get('title', '')}\n\nAbstract: {abstract}"
    return ""


_NUMBER = re.compile(r"(?<![\w.])(?:\d{1,3}\.\d{1,4}|0?\.\d{2,4})(?![\w.])")
_TABLE_CAPTION = re.compile(r"\bTable\s+(?:\d{1,2}|[IVX]{1,4})\b", re.IGNORECASE)


def tables_missing(text: str) -> bool:
    """True when a page names its tables but their contents are not in the text.

    Publisher pages often render tables as images (Frontiers) or load them separately
    (IEEE), so the scraped text keeps "Table 2" captions with no numbers after them --
    and step 1.8 cannot extract results that are only in those tables.
    """
    ends = [m.end() for m in _TABLE_CAPTION.finditer(text or "")]
    if len(ends) < 2:
        return False
    empty = sum(1 for e in ends if len(_NUMBER.findall(text[e:e + 600])) < 4)
    return empty / len(ends) >= 0.6


# Section headings a full paper has and a landing page (abstract + page chrome) lacks.
_SECTIONS = [re.compile(p, re.IGNORECASE) for p in (
    r"\bintroduction\b",
    r"\b(?:materials and methods|methods?|methodology|proposed method|approach)\b",
    r"\b(?:results|experiments?|evaluation)\b",
    r"\b(?:discussion|conclusions?)\b",
    r"\b(?:references|bibliography)\b")]
ABSTRACT_ONLY = "[abstract only]"


def _section_count(text: str) -> int:
    return sum(bool(p.search(text or "")) for p in _SECTIONS)


def looks_like_landing_page(text: str) -> bool:
    """Worth looking for a fuller copy: short, or missing a full paper's sections.

    In the January 2026 run most "full text" was a publisher landing page -- the
    abstract plus page chrome (IEEE median 3.6k chars, arXiv /abs/ 5.4k, CVF 2.8k) --
    and step 1.8 found metrics for 31% of those papers against 79% with real text.
    Broad on purpose: it only triggers a lookup, and the page is kept unless the
    copy found is a fuller paper.
    """
    return len(text or "") < 15000 or _section_count(text) < 4


def is_abstract_only(text: str) -> bool:
    """Too little of the paper to trust for results: under 8k chars, or at most two
    of a paper's sections (0 of the 85 real full texts in the human gold qualify)."""
    return len(text or "") < 8000 or _section_count(text) <= 2


def direct_pdf_url(url: str) -> str:
    """The free PDF for links that point at an abstract page of a known open server:
    arXiv /abs/ -> /pdf/, CVF open access .html -> the paper .pdf, bioRxiv/medRxiv ->
    .full.pdf. "" for anything else."""
    u = (url or "").strip()
    m = re.match(r"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/([^?#]+?)(?:\.pdf)?/?(?:[?#].*)?$", u)
    if m:
        return f"https://arxiv.org/pdf/{m.group(1)}"
    m = re.match(r"^(https?://openaccess\.thecvf\.com/.+)/html/(.+?_paper)\.html$", u)
    if m:
        return f"{m.group(1)}/papers/{m.group(2)}.pdf"
    m = re.match(r"^(https?://(?:www\.)?(?:biorxiv|medrxiv)\.org/content/10\.1101/[^?#]+?)"
                 r"(?:\.abstract|\.full(?:-text)?|\.full\.pdf)?/?(?:[?#].*)?$", u)
    if m:
        return f"{m.group(1)}.full.pdf"
    return ""


def _semantic_scholar_hit(item: dict) -> dict:
    """Semantic Scholar's record for the paper (best title match; {} if none), once per title.
    Its key allows 1 request a second across every endpoint, for all of a run's processes;
    polite_get paces them (WSI_S2_INTERVAL) and the answers are cached."""
    title = (item.get("title") or "").lstrip("… ").strip()
    if not title:
        return {}
    hit = lookup_cache.get("s2_match", _norm_title(title))
    if hit is not lookup_cache.MISS:
        return hit
    headers = {"x-api-key": os.environ["S2_API_KEY"]} if os.getenv("S2_API_KEY") else {}
    try:
        # 429s are routine (it answers them erratically even under its limit): back off.
        for wait in (1.5, 4.0, 10.0, None):
            r = polite_get("https://api.semanticscholar.org/graph/v1/paper/search/match",
                           params={"query": title, "fields": "title,openAccessPdf,externalIds,abstract"},
                           headers=headers, timeout=20)
            if r.status_code != 429 or wait is None:
                break
            time.sleep(wait + random.random())
        if r.status_code not in (200, 404):
            return {}  # not cached: asked again next time
        hit = (r.json().get("data") or [{}])[0] if r.status_code == 200 else {}
    except Exception as exc:  # noqa: BLE001 - a fallback must never fail the paper
        logger.debug("Semantic Scholar lookup failed for %s: %s", title, exc)
        return {}
    hit = hit if _same_title(hit.get("title"), title) else {}
    lookup_cache.put("s2_match", _norm_title(title), hit)
    return hit


def _semantic_scholar_pdf_urls(item: dict) -> list:
    """Open-access PDF links Semantic Scholar knows for the paper (best match by title)."""
    hit = _semantic_scholar_hit(item)
    urls = [((hit.get("openAccessPdf") or {}).get("url") or "")]
    arxiv = (hit.get("externalIds") or {}).get("ArXiv")
    if arxiv:
        urls.append(f"https://arxiv.org/pdf/{arxiv}")
    return [u for u in urls if u]


def _europe_pmc_text(link: str) -> str:
    """Full text of a PMC article from Europe PMC. pmc.ncbi.nlm.nih.gov answers scrapers
    with 403 (all 7 PMC links in the 2026-10-27 smoke run); Europe PMC serves the
    open-access subset as XML."""
    import html as _html

    m = re.search(r"PMC\d+", link or "")
    if not m:
        return ""
    try:
        r = polite_get(f"https://www.ebi.ac.uk/europepmc/webservices/rest/{m.group()}/fullTextXML", timeout=30)
    except Exception:  # noqa: BLE001 - a fallback must never fail the paper
        return ""
    if r.status_code != 200:
        return ""
    body = re.sub(r"<(ref-list|back)\b.*", "", r.text, flags=re.S)  # drop the reference list
    text = " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", body)).split())
    return text if len(text) >= MIN_FULLTEXT_CHARS else ""


def open_access_pdf_text(item: dict, use_browser: bool = False) -> str:
    """Text of the first open-access PDF that parses: those OpenAlex lists, then those
    Semantic Scholar lists ("" if none)."""
    w = _openalex_work(item)
    urls = [(loc or {}).get("pdf_url") for loc in [w.get("best_oa_location")] + list(w.get("oa_locations") or [])] \
        if w else []
    urls = [u for u in urls if u]
    tried = set()
    for source in (lambda: urls, lambda: _semantic_scholar_pdf_urls(item)):
        for url in source():
            if url in tried:
                continue
            tried.add(url)
            try:
                text = (extract_text_from_url(url, use_browser=use_browser) or "").strip()
            except Exception:  # noqa: BLE001 - a fallback must never fail the paper
                text = ""
            if len(text) >= MIN_FULLTEXT_CHARS:
                return text
    return ""


_PDF_HREF = re.compile(r'href="([^"]+?\.pdf(?:\?[^"]*)?)"', re.IGNORECASE)
_MAX_PDF_BYTES = 30_000_000  # the OpenAI file-input limit is 32 MB


def _get_pdf(url: str) -> bytes:
    """The PDF at `url`, or at the first full-paper PDF link of the HTML page there
    (repositories such as IRIS / DSpace put the paper behind a landing page)."""
    import html as _html
    from urllib.parse import urljoin

    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = polite_get(url, timeout=60, headers=headers)
    except Exception:  # noqa: BLE001 - a fallback must never fail the paper
        return b""
    if r.status_code != 200 or r.blocked:
        return b""
    if r.content[:5] == b"%PDF-":
        return r.content if len(r.content) < _MAX_PDF_BYTES else b""
    if "html" not in r.headers.get("Content-Type", ""):
        return b""
    links = [h for h in _PDF_HREF.findall(r.text) if "supp" not in h.lower()]
    for href in dict.fromkeys(links[:3]):
        try:
            pdf = polite_get(urljoin(r.url, _html.unescape(href)), timeout=60, headers=headers).content
        except Exception:  # noqa: BLE001
            continue
        if pdf[:5] == b"%PDF-" and len(pdf) < _MAX_PDF_BYTES:
            return pdf
    return b""


def _unpaywall_urls(doi: str) -> list:
    """Open-access PDF links, then repository pages, Unpaywall knows for a DOI (no key)."""
    email = os.getenv("UNPAYWALL_EMAIL") or contact_email() or "wsi-hub-pipeline@example.org"
    try:
        r = polite_get(f"https://api.unpaywall.org/v2/{doi}", params={"email": email}, timeout=20)
        d = r.json() if r.status_code == 200 else {}
    except Exception:  # noqa: BLE001
        return []
    locs = [d.get("best_oa_location")] + list(d.get("oa_locations") or [])
    pdfs = [(loc or {}).get("url_for_pdf") for loc in locs]
    pages = [(loc or {}).get("url") for loc in locs if (loc or {}).get("host_type") == "repository"]
    return [u for u in dict.fromkeys(pdfs + pages) if u]


def open_access_pdf(item: dict) -> bytes:
    """The paper's open-access PDF as bytes (b"" if none): the PDFs OpenAlex lists, then
    Semantic Scholar's, then Unpaywall's PDFs and repository copies. For papers whose
    tables are images, which no text extraction recovers, so the model has to read the
    PDF itself (IEEE Access typesets its tables as images even in the PDF)."""
    cached = _pdf_cache_path(item)
    if cached.exists():
        return cached.read_bytes()
    # Fetched once: repositories throttle repeat downloads (IRIS returned 403 after a
    # few fetches in one day), and every eval and rerun asks for the same PDFs.
    pdf = _find_open_access_pdf(item)
    if pdf and not cached.exists():
        _store_pdf(cached, pdf)
    return pdf


def _pdf_cache_path(item: dict) -> Path:
    import hashlib
    cache = Path(os.getenv("WSI_PDF_CACHE", "pipeline/data/pdf_cache"))
    key = hashlib.sha1((item.get("link") or item.get("title") or "").encode()).hexdigest()
    return cache / f"{key}.pdf"


def _store_pdf(path: Path, pdf: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_bytes(pdf)
    replace_retry(tmp, path)  # a reader never sees half a PDF


def _find_open_access_pdf(item: dict) -> bytes:
    w = _openalex_work(item)
    locs = [w.get("best_oa_location")] + list(w.get("oa_locations") or []) if w else []
    urls = [(loc or {}).get("pdf_url") for loc in locs]
    s2 = _semantic_scholar_pdf_urls(item)
    urls += [u for u in s2 if "doi.org/" not in u]
    doi = (item.get("doi") or (w or {}).get("doi") or next((u for u in s2 if "doi.org/" in u), "") or "")
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi.strip(), flags=re.IGNORECASE)
    if doi:
        urls += _unpaywall_urls(doi)
    for url in [u for u in dict.fromkeys(urls) if u][:8]:
        pdf = _get_pdf(url)
        if pdf:
            return pdf
    return openalex_content_pdf(item)


def openalex_content_pdf(item: dict) -> bytes:
    """OpenAlex's stored copy of the paper's PDF (b"" if it has none), kept in the PDF cache.

    A download costs 100 credits ($0.01), from the key's free daily credits and then its
    prepaid balance; asking for a paper OpenAlex has no copy of is free. The copy comes from
    OpenAlex's own store, so it is there when the publisher blocks us: in the 2026-10-29
    smoke runs it had 73 of the 227 papers we got only abstracts for (ScienceDirect, MDPI,
    IEEE, Wiley...). WSI_OPENALEX_CONTENT=0 turns the downloads off.
    """
    cached = _pdf_cache_path(item)
    if cached.exists():
        return cached.read_bytes()
    if os.getenv("WSI_OPENALEX_CONTENT", "1").lower() in ("0", "false", "no"):
        return b""
    w = _openalex_work(item)
    if not (w.get("has_content") or {}).get("pdf") or not w.get("id"):
        return b""
    wid = w["id"].rsplit("/", 1)[-1]
    if float(host_state(_OPENALEX).get("content_blocked_until", 0)) > time.time() \
            or lookup_cache.get("openalex_content", wid) == "unusable":
        return b""
    # Once more if the transfer broke off (a 2 MB copy did, among 8 parallel downloads).
    for _ in range(2):
        r = _openalex_get(f"https://content.openalex.org/works/{wid}.pdf", {})
        if r is not None:
            break
    if r is None or r.status_code != 200:
        return b""
    pdf = r.content
    # Some of OpenAlex's copies are cut off at 26 MiB (2 of 15 in a 2026-10-29 test): no
    # end-of-file marker, and no text. Paid for all the same, so never asked for again.
    usable = pdf[:5] == b"%PDF-" and b"%%EOF" in pdf[-2048:] and len(pdf) < _MAX_PDF_BYTES
    log_event("openalex_content", link=item.get("link") or "", work=wid, bytes=len(pdf),
              **({} if usable else {"unusable": True}))
    if not usable:
        lookup_cache.put("openalex_content", wid, "unusable")
        return b""
    _store_pdf(cached, pdf)
    return pdf


def with_openalex_copy(item: dict, text: str) -> str:
    """`text`, or the text of OpenAlex's copy of the paper (openalex_content_pdf) when `text`
    is less than the full paper -- an abstract, a stub, the title. For papers the gate keeps:
    the gate decides on the abstract as well as on full text, but step 1.8 found metrics in
    88% of full texts and 48% of abstracts, so the download is paid only for papers kept."""
    if text_source(text) == "full_text" or os.getenv("WSI_OPEN_ACCESS_FALLBACK", "1").lower() in ("0", "false", "no"):
        return text
    try:
        pdf = openalex_content_pdf(item)
    except Exception as exc:  # noqa: BLE001 - a fallback must never fail the paper (a full disk, say)
        logger.warning("OpenAlex copy failed for %s: %s", item.get("link") or item.get("title", ""), exc)
        return text
    full = clean_text(pdf_text(pdf)).strip() if pdf else ""
    if text_source(full) != "full_text":
        return text
    log_event("fetch_upgrade", link=item.get("link") or item.get("url") or "", was=text_source(text), chars=len(full))
    return full


def with_table_text(item: dict, text: str, use_browser: bool = False) -> str:
    """`text`, or the open-access PDF when `text` lost its tables and the PDF carries more
    numbers (results). WSI_PDF_FOR_TABLES=0 disables."""
    if (len(text) < MIN_FULLTEXT_CHARS or not tables_missing(text)
            or os.getenv("WSI_PDF_FOR_TABLES", "1").lower() in ("0", "false", "no")):
        return text
    pdf = open_access_pdf_text(item, use_browser=use_browser)
    return pdf if len(_NUMBER.findall(pdf)) > len(_NUMBER.findall(text)) else text


def clean_text(text: str) -> str:
    """Text without lone surrogates or NULs. Some pages and PDFs decode to them; the
    OpenAI client can't encode a surrogate (the call fails and the gate drops the paper)
    and Postgres rejects a NUL (the push fails)."""
    return (text or "").encode("utf-8", "ignore").decode("utf-8").replace("\x00", "")


def load_full_text(item: dict, use_browser: bool = False) -> str:
    """_load_full_text, timed: each fetch goes to the WSI_USAGE_LOG (when set)."""
    t0 = time.time()
    text = clean_text(_load_full_text(item, use_browser))
    log_event("fetch", link=item.get('link') or item.get('url') or "", seconds=round(time.time() - t0, 2),
              chars=len(text), source=text_source(text))
    return text


def _load_full_text(item: dict, use_browser: bool = False) -> str:
    """Fetches the full text for a paper record.

    Order: ``item['pdf_url']``, the free PDF behind an arXiv / CVF / bioRxiv / medRxiv
    abstract link (``direct_pdf_url``), then the paper link; under MIN_FULLTEXT_CHARS,
    the open-access copy or abstract from OpenAlex (``open_access_text``). A landing
    page (``looks_like_landing_page``) is replaced by an open-access PDF (OpenAlex,
    then Semantic Scholar) when that is the fuller paper, and a page whose tables did
    not come through (``tables_missing``) by one with more numbers. Text that is still
    only the abstract is prefixed "[abstract only]". Returns "" only when nothing is
    found, which the full-text gate treats as a drop. WSI_OPEN_ACCESS_FALLBACK=0 turns
    the open-access lookups and PDF rewrites off.
    """

    pdf_url = item.get('pdf_url')
    link_url = item.get('link') or item.get('url')
    fallbacks_on = os.getenv("WSI_OPEN_ACCESS_FALLBACK", "1").lower() not in ("0", "false", "no")
    text = ""

    def fetch(url: str) -> str:
        try:
            return (extract_text_from_url(url, use_browser=use_browser) or "").strip()
        except Exception as exc:  # pragma: no cover - defensive, should not raise
            logger.warning("Failed to extract text from %s: %s", url, exc)
            return ""

    # The paper's PDF when we have it, or when the link is an open server's abstract page.
    for url in (pdf_url, direct_pdf_url(link_url) if fallbacks_on else ""):
        if url and not text:
            text = fetch(url)

    if not text and link_url:
        text = fetch(link_url)

    # Other routes to the same paper (alt_links, from deduplication): a paper merged from a
    # PMC page and a conference PDF was read from the PMC abstract while the PDF went unread.
    for alt in item.get('alt_links') or []:
        if len(text) >= MIN_FULLTEXT_CHARS and not is_abstract_only(text) and not looks_like_landing_page(text):
            break
        for url in dict.fromkeys(u for u in (direct_pdf_url(alt) if fallbacks_on else "", alt) if u):
            candidate = fetch(url)
            if len(candidate) > len(text) and not is_abstract_only(candidate):
                text = candidate
                break

    if len(text) < MIN_FULLTEXT_CHARS and fallbacks_on:
        fallback = open_access_text(item, use_browser=use_browser)
        if len(fallback) > len(text):
            text = fallback

    # A landing page (abstract + page chrome) passes the length check: look for an
    # open-access copy, and keep it only if it is the fuller paper.
    if fallbacks_on and text and looks_like_landing_page(text):
        pdf = open_access_pdf_text(item, use_browser=use_browser)
        if pdf and len(pdf) > len(text) and _section_count(pdf) >= _section_count(text):
            text = pdf

    text = with_table_text(item, text, use_browser=use_browser)

    # A stub (landing page, book preview) still too short to judge: put the title and
    # snippet in front so the gate sees what the paper is about.
    if 0 < len(text) < MIN_FULLTEXT_CHARS and not text.startswith(ABSTRACT_ONLY):
        text = (f"[partial text]\nTitle: {(item.get('title') or '').strip()}\n"
                f"Snippet: {(item.get('snippet') or '').strip()}\n\n{text}")

    # Nothing anywhere: judge on title + snippet rather than drop the paper unjudged.
    # Dropping every paper whose page could not be fetched cost relevant papers on the
    # human gold. WSI_FULLTEXT_FALLBACK_SNIPPET=0 restores the drop.
    if not text and os.getenv('WSI_FULLTEXT_FALLBACK_SNIPPET', '1').lower() not in {'0', 'false', 'no'}:
        title, snippet = (item.get('title') or '').strip(), (item.get('snippet') or '').strip()
        if title or snippet:
            text = f"[title and snippet only]\nTitle: {title}\n\nSnippet: {snippet}"

    # Still only the abstract page after every fallback (mostly paywalled papers): say
    # so, so later steps and the site know results from it come from the abstract.
    if text and not text.startswith("[") and is_abstract_only(text):
        text = f"{ABSTRACT_ONLY}\n{text}"

    return text


def text_source(text: str) -> str:
    """What a paper's text is: full_text, abstract, partial, title_snippet or none."""
    t = text or ""
    if not t:
        return "none"
    if t.startswith(ABSTRACT_ONLY):
        return "abstract"
    if t.startswith("[partial text]"):
        return "partial"
    if t.startswith("[title and snippet only]"):
        return "title_snippet"
    return "abstract" if is_abstract_only(t) else "full_text"


def gate_evidence(text: str, content: str | None, reason: str) -> dict:
    """What the gate decided on, for the decision ledger: the text as judged (archived by
    hash, before any later upgrade to a fuller copy), its kind, and the answer per criterion.
    reason: kept | criteria (a criterion failed) | unparsable | no_text | call_error."""
    from pipeline.src.provenance import archive_text
    answers = None
    if content:
        try:
            from pipeline.utils.llms.llm import strip_json_wrapper
            answers = json.loads(strip_json_wrapper(content))
        except (ValueError, TypeError):
            answers = None
    return {"reason": reason, "text_source": text_source(text), "text_chars": len(text or ""),
            "text_sha256": archive_text("gate_text", text) if text else "",
            "answers": answers if isinstance(answers, dict) else None,
            **({"raw_answer": content[:2000]} if content and not isinstance(answers, dict) else {})}


def _gate_reason(decision: bool | None) -> str:
    return "kept" if decision else ("unparsable" if decision is None else "criteria")


def _judge_fulltext_item(args: tuple) -> dict:
    """Fetch one paper's text and run the gate: {"keep": True, "item": <paper + fulltext>},
    {"keep": False}, or {"error": ...} when the gate call failed (retried, not a drop).
    Every final answer also carries "gate": gate_evidence(...)."""

    item, _, _, prompt_t, pos, neg, use_gemini, use_browser = args
    text = (load_full_text(item, use_browser=use_browser) or "").strip()

    if not text:
        return {"keep": False, "gate": gate_evidence("", None, "no_text")}

    # One JSON answer with a bool per criterion.
    structured_prompt = _render_structured(prompt_t, pos, neg)
    model = (os.getenv('WSI_YESNO_MODEL',
                       'gemini-3.1-flash-lite' if use_gemini else 'gpt-6-luna'))
    try:
        # Non-batch run_gpt does not plumb thinking_budget; the batch path
        # (production) sets thinking_budget=0. This sync path is a fallback.
        resp = run_gpt(
            system_prompt=structured_prompt,
            user_prompt=text,
            model=model,
            response_format={"type": "json_object"},
            use_gemini=use_gemini,
            item=item.get('link') or item.get('title') or "",
        )
        content = resp.choices[0].message.content
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Structured gate call failed: %s", exc)
        from pipeline.src.checkpoint import is_permanent_error
        if is_permanent_error(exc):
            return {"keep": False, "call_error": f"{type(exc).__name__}: {exc}"[:300],  # final
                    "gate": gate_evidence(text, None, "call_error")}
        return {"error": f"{type(exc).__name__}: {exc}"[:300], "gate": gate_evidence(text, None, "call_error")}
    decision = _decide_structured_gate(content, len(pos), len(neg))
    gate = gate_evidence(text, content, _gate_reason(decision))
    if not decision:
        return {"keep": False, "gate": gate}
    enriched = item.copy()
    enriched['fulltext'] = with_openalex_copy(item, text)
    return {"keep": True, "item": enriched, "gate": gate}


# A paper the gate never judged on its content: the call failed, or the answer was missing
# or unreadable. The run drops it like a rejection, but it is not one.
_NOT_JUDGED = ("call_error", "unparsable")


def decision_status(d: dict) -> str:
    """kept | rejected (a criterion failed, or no text to judge) | error (see _NOT_JUDGED)
    | unjudged (no decision recorded)."""
    if not d:
        return "unjudged"
    if d.get("keep"):
        return "kept"
    return "error" if "error" in d or (d.get("gate") or {}).get("reason") in _NOT_JUDGED else "rejected"


def write_gate_decisions(out: str, items: list[dict], decisions: dict) -> None:
    """`<out stem>_decisions.json`: one row per candidate with how it was found, its
    decision_status() and gate_evidence(). The audit of rejected papers and the flow diagram
    are built from it. Written before the stage's output, so an output never lacks it."""
    from .dedupe import dedupe_key
    rows, seen = [], set()
    for it in items:
        k = dedupe_key(it)
        if k in seen:
            continue
        seen.add(k)
        d = decisions.get(k) or {}
        rows.append({"key": k, "link": it.get('link'), "title": it.get('title'), "year": it.get('year'),
                     "source": it.get('source'), "provenance": it.get('provenance'), "seed": it.get('seed'),
                     "keep": bool(d.get("keep")), "status": decision_status(d),
                     **({"call_error": d.get("call_error") or d["error"]} if d.get("call_error") or d.get("error") else {}),
                     "gate": d.get("gate")})
    path = Path(out).with_name(Path(out).stem + "_decisions.json")
    atomic_json(path, rows, indent=1)


def _process_fulltext_item(args: tuple) -> dict | None:
    """The paper with its fulltext when the gate keeps it, else None (a failed call too).

    ``args`` is ``(item, _, _, prompt_t, pos, neg, use_gemini, use_browser)``; the two
    unused slots keep the tuple layout the evaluation scorers pass.
    """
    return _judge_fulltext_item(args).get("item")


def run_fulltext_filters(
    items: list[dict],
    prompt_path: str,
    out: str | None = None,
    use_gemini: bool = False,
    use_batch: bool = False,
    use_browser: bool = False,
    step_name: str = "fulltext_filter",
) -> list[dict]:
    """Loads each paper's full text and keeps the papers that meet the criteria.

    One call per paper (``check_all_criteria_structured.md``) answers every
    criterion in criteria.txt; a paper is kept iff all positive criteria hold and
    no negative one does.

    ``step_name`` is the batch cache key and MUST be unique per pipeline stage --
    this runs as steps 1.2, 1.3b and 1.6b. Batch custom_ids are positional
    indices, so sharing one key lets a later, smaller stage match every id in an
    earlier stage's cache and silently receive that stage's answers for different
    papers.

    Args:
        items: List of paper records to filter.
        prompt_path: Path to the structured gate prompt.
        out: Optional output path for results.
        use_gemini: If True, use Gemini API instead of OpenAI.
        use_batch: If True, use batch API for LLM calls.
    """

    if not items:
        if out:
            write_gate_decisions(out, [], {})
            atomic_json(out, [], indent=4)
        return []

    prompt_template = Path(prompt_path).read_text(encoding="utf-8")

    pos, neg = _load_criteria()

    worker_args = [
        (item, None, None, prompt_template, pos, neg, use_gemini, use_browser)
        for item in items
    ]
    disable_mp = os.getenv('WSI_DISABLE_FULLTEXT_MP', '').lower() in {'1', 'true', 'yes'}
    use_multiprocessing = not disable_mp and len(items) > 1

    # Announce stage with counts for clearer console separation
    try:
        print(f"Full-text filtering — processing {len(items)} items...")
    except Exception:
        pass

    # Batch mode: load every paper's text, then submit one batch
    if use_batch:
        # Load full text for all items first, in threads (fetching is I/O; one at a time
        # it was ~1.65 s a paper, 9-16 h for a full run). WSI_FETCH_WORKERS sets the count.
        from concurrent.futures import ThreadPoolExecutor
        items_with_text: list[tuple[int, dict, str]] = []
        # The headless browser fallback (--browser) isn't thread-safe: one worker then.
        workers = 1 if use_browser else int(os.getenv("WSI_FETCH_WORKERS", "16"))
        # Round-robin across publishers, so the threads don't all queue on one site.
        order = interleave_by_host(list(range(len(items))), lambda i: items[i].get('link') or "")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            texts = dict(zip(order, tqdm(ex.map(lambda i: load_full_text(items[i], use_browser=use_browser), order),
                                         total=len(items), desc="Loading full text")))
        for i, item in enumerate(items):
            if texts[i]:
                items_with_text.append((i, item, texts[i]))

        from .dedupe import dedupe_key
        decisions = {dedupe_key(item): {"keep": False, "gate": gate_evidence("", None, "no_text")}
                     for i, item in enumerate(items) if not texts[i]}
        passed: list[dict] = []
        if items_with_text:
            # ONE request per item returning a bool per criterion.
            if use_gemini:
                model_name = os.getenv('WSI_YESNO_MODEL', 'gemini-3.1-flash-lite')
            else:
                model_name = os.getenv('WSI_YESNO_MODEL', 'gpt-6-luna')

            structured_prompt = _render_structured(prompt_template, pos, neg)
            requests = [
                {
                    'custom_id': f'gate:{i}',
                    'system_prompt': structured_prompt,
                    'user_prompt': text_val,
                    'model': model_name,
                    'temperature': 0,
                    'thinking_budget': 0,
                    'response_format': {'type': 'json_object'},
                }
                for i, _item, text_val in items_with_text
            ]
            print(f"Submitting single structured-gate batch: {len(requests)} queries...")
            results = run_batch_requests(
                requests=requests, step_name=step_name,
                use_gemini=use_gemini, model=model_name,
            )
            for i, item, text_val in items_with_text:
                content = _extract_batch_content(results.get(f'gate:{i}'), use_gemini)
                decision = _decide_structured_gate(content, len(pos), len(neg))
                # No answer at all (the batch item errored or is missing) is a failed call.
                reason = "call_error" if content is None else _gate_reason(decision)
                decisions[dedupe_key(item)] = {"keep": bool(decision),
                                               "gate": gate_evidence(text_val, content, reason)}
                if decision:
                    enriched = item.copy()
                    enriched['fulltext'] = with_openalex_copy(item, text_val)
                    passed.append(enriched)

        if out:
            write_gate_decisions(out, items, decisions)
            atomic_json(out, passed, indent=4)

        return passed

    # Papers go out round-robin across publishers (Scholar lists them clustered by site),
    # each finished one is checkpointed, and a failed gate call is retried rather than
    # counted as a drop. A drop is counted, not diagnosed: the fetch log (WSI_USAGE_LOG)
    # records each fetch's length. WSI_FULLTEXT_WORKERS sets the pool size.
    from pipeline.src.checkpoint import fingerprint, process_runner, run_items
    from .dedupe import dedupe_key
    # 16: Semantic Scholar's 1 request a second (about one per paper) caps the gate near
    # 0.85 papers/s, which ~12 workers already reach; more only queue for it.
    workers = int(os.getenv("WSI_FULLTEXT_WORKERS", "16")) if use_multiprocessing else 1
    ordered = interleave_by_host(worker_args, lambda a: a[0].get('link') or a[0].get('pdf_url') or "")
    model = os.getenv('WSI_YESNO_MODEL', 'gemini-3.1-flash-lite' if use_gemini else 'gpt-6-luna')
    done, still_failing = run_items(
        step_name, ordered, key=lambda a: dedupe_key(a[0]), fn=_judge_fulltext_item,
        runner=process_runner(workers, desc="Full-text filtering"),
        failed=lambda res: "error" in res,
        fp=fingerprint(_render_structured(prompt_template, pos, neg), model))
    decisions = dict(done)
    if still_failing:
        from .metrics_extractor import FAILED_CALLS
        for args, res in still_failing:
            decisions[dedupe_key(args[0])] = res if isinstance(res, dict) else {"error": str(res)[:300]}
            FAILED_CALLS.append({"step": step_name, "paper_id": args[0].get('link') or args[0].get('title', ''),
                                 "task_type": "", "error": "gate call failed after retries"})
    keys = list(dict.fromkeys(dedupe_key(it) for it in items))
    passed = [done[k]["item"] for k in keys if (done.get(k) or {}).get("keep")]
    failed_gate_all = len(items) - len(passed)

    if out:
        write_gate_decisions(out, items, decisions)
        atomic_json(out, passed, indent=4)

    # Emit diagnostics
    try:
        logger.info(
            "Full-text filtering diagnostics: total=%d, passed=%d, dropped=%d (no text or a gate)",
            len(items), len(passed), failed_gate_all
        )
    except Exception:
        pass

    return passed
