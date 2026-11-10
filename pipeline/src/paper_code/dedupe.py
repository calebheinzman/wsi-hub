import hashlib
import json
import re
from urllib.parse import parse_qs, urlencode, urlparse
from typing import Dict, List, Optional, Set

# Query parameters that only track the visit. Every other parameter is kept: on PLOS
# (?id=10.1371/...), IEEE (stamp.jsp?arnumber=), OpenReview (forum?id=) and SciELO
# (scielo.php?pid=) the query is what names the paper, and dropping it merged different
# papers into one.
_TRACKING = re.compile(r"^(utm.*|casa_token|via|fbclid|gclid|ref|referrer|src|source|cookieset|"
                       r"sessionid|jsessionid|sid|tab|lang|hl|ver|version|download|trk|uuid|rss|"
                       r"pq-origsite|cbl|diss|oi|pg|ots|sig|dq|lr|ved|ei)$", re.IGNORECASE)


def normalize_url(url: str) -> str:
    """
    Normalizes a URL: drops the fragment and tracking parameters, keeps (sorted) the
    parameters that identify the page.

    Args:
        url: The URL to normalize.

    Returns:
        The normalized URL.
    """
    if not url:
        return ""
    parsed = urlparse(url)
    query = sorted((k, v) for k, vs in parse_qs(parsed.query, keep_blank_values=False).items()
                   for v in vs if not _TRACKING.match(k))
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return base + ("?" + urlencode(query) if query else "")

def dedupe_key(item: dict) -> str:
    """
    Generates a unique key for a paper record.

    Args:
        item: The paper record.

    Returns:
        A unique key for the paper.
    """
    if 'link' in item and item['link']:
        return normalize_url(item['link'])
    if 'pdf_url' in item and item['pdf_url']:
        return normalize_url(item['pdf_url'])

    title = item.get('title', '').lower().strip()
    year = item.get('year', '')
    authors = item.get('authors', [])
    first_author = authors[0].lower().strip() if authors else ''

    return hashlib.sha1(f"{title}{first_author}{year}".encode()).hexdigest()

# --- Same paper, different URL --------------------------------------------------------
# A URL key alone counts one paper twice when two routes reach it: a PDF and its article
# page, an arXiv preprint and its MICCAI version, a thesis on a repository and on ProQuest
# (22 groups in the January corpus). Records are the same paper when they share a URL, a
# DOI, an arXiv id, or a normalized title -- a title match only when the title is specific
# (30+ characters), the years agree within one, and the author lists share a surname.

_TITLE_JUNK = re.compile(
    r"^\s*\[(?:pdf|html|citation|book|b)\]\s*"
    r"|\s*author\s*&\s*article\s*details\s*$"
    r"|\s*[-|]\s*(?:pmc|springerlink|sciencedirect|ieee xplore|researchgate|semantic scholar|europe pmc)\s*$"
    r"|\s*(?:…|\.\.\.)\s*$",
    re.IGNORECASE,
)
_DOI = re.compile(r"10\.\d{4,9}/[^\s?#&]+", re.IGNORECASE)
_ARXIV = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", re.IGNORECASE)
_YEAR = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")
MIN_TITLE_CHARS = 30


def title_key(title: str) -> str:
    """Lowercase alphanumerics of a title without scraping junk; "" if too short to trust."""
    t = title or ""
    for _ in range(3):  # junk can stack ("[PDF] ... - PMC")
        t = _TITLE_JUNK.sub("", t)
    t = re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()
    return t if len(t) >= MIN_TITLE_CHARS else ""


def paper_year(item: dict) -> Optional[int]:
    years = _YEAR.findall(str(item.get("year") or ""))
    return int(years[0]) if years else None


def surnames(item: dict) -> Set[str]:
    """Authors' surnames (accents folded), from a list or a stringified list of names."""
    import unicodedata
    authors = item.get("authors") or []
    if isinstance(authors, str):
        try:
            import ast
            authors = ast.literal_eval(authors)
        except (ValueError, SyntaxError):
            authors = [a for a in re.split(r",|;| and ", authors) if a.strip()]
    if not isinstance(authors, list):
        return set()
    out = set()
    for a in authors:
        name = a if isinstance(a, str) else str((a or {}).get("name", ""))
        name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
        words = re.sub(r"[^a-z -]", " ", name).replace("-", " ").split()
        if words and len(words[-1]) > 1:
            out.add(words[-1])
    return out


def paper_ids(item: dict) -> Set[str]:
    """URL, DOI and arXiv keys of a record."""
    keys = set()
    for field in ("link", "pdf_url"):
        if item.get(field):
            keys.add("url:" + normalize_url(item[field]))
    text = " ".join(str(item.get(f) or "") for f in ("doi", "link", "pdf_url"))
    for doi in _DOI.findall(text):
        keys.add("doi:" + re.sub(r"(\.pdf|/full|/abstract|[.,;)])$", "", doi.lower()))
    for arxiv in _ARXIV.findall(text):
        keys.add("arxiv:" + arxiv)
    if not item.get("link") and not item.get("pdf_url"):
        # No URL: the exact title, first author and year, as dedupe_key has always done.
        keys.add("tay:" + dedupe_key(item))
    return keys


def _compatible(a: dict, b: dict) -> bool:
    ya, yb = paper_year(a), paper_year(b)
    if ya and yb and abs(ya - yb) > 1:
        return False
    # Author lists are partial and reordered (Scholar shows the first few; repositories
    # list differently), so any shared surname is enough.
    sa, sb = surnames(a), surnames(b)
    return not (sa and sb and not (sa & sb))


def _text_rank(item: dict) -> int:
    t = item.get("fulltext") or ""
    if not t:
        return -1
    for rank, prefix in enumerate(("[title and snippet only]", "[partial text]", "[abstract only]")):
        if t.startswith(prefix):
            return rank
    return 3


def _citations(item: dict) -> int:
    try:
        return int(str(item.get("citations") or 0).replace(",", ""))
    except ValueError:
        return 0


def prefer_record(kept: dict, incoming: dict) -> dict:
    """The better of two records of one paper (fuller text, then more citations), with the
    other's missing fields filled in."""
    better, other = ((incoming, kept) if (_text_rank(incoming), _citations(incoming)) >
                     (_text_rank(kept), _citations(kept)) else (kept, incoming))
    merged = dict(better)
    for field, value in other.items():
        if field not in merged or merged[field] in (None, "", [], {}):
            merged[field] = value
    return merged


def _route(item: dict) -> dict:
    return {k: item.get(k) for k in ("link", "source", "provenance", "seed")}


class PaperIndex:
    """Records with one entry per paper; find() and add() match on any shared key."""

    def __init__(self, items: Optional[List[dict]] = None, merge=None):
        self.records: List[dict] = []
        self._by_key: Dict[str, int] = {}
        self._by_title: Dict[str, List[int]] = {}
        self._merge = merge or prefer_record
        for item in items or []:
            self.add(item)

    def find(self, item: dict) -> Optional[int]:
        for key in paper_ids(item):
            if key in self._by_key:
                return self._by_key[key]
        t = title_key(item.get("title", ""))
        for idx in self._by_title.get(t, []) if t else []:
            if _compatible(self.records[idx], item):
                return idx
        return None

    def _register(self, item: dict, idx: int) -> None:
        for key in paper_ids(item):
            self._by_key.setdefault(key, idx)
        t = title_key(item.get("title", ""))
        if t and idx not in self._by_title.setdefault(t, []):
            self._by_title[t].append(idx)

    def add(self, item: dict) -> bool:
        """Add a record; True if it is a new paper, False if it merged into one."""
        idx = self.find(item)
        if idx is None:
            self.records.append(item)
            self._register(item, len(self.records) - 1)
            return True
        kept = self.records[idx]
        merged = self._merge(kept, item)
        # Keep every route to the paper: the merged record's link plus the others'.
        links = [kept.get("link"), item.get("link"), *kept.get("alt_links", []), *item.get("alt_links", [])]
        alt = [u for u in dict.fromkeys(links) if u and u != merged.get("link")]
        if alt:
            merged["alt_links"] = alt
        # And every way it was found (which query and rank, which citing seed): the merged
        # record keeps one record's provenance, and the flow diagram counts all of them.
        routes = [*(kept.get("routes") or [_route(kept)]), *(item.get("routes") or [_route(item)])]
        merged["routes"] = list({json.dumps(r, sort_keys=True, default=str): r for r in routes}.values())
        self.records[idx] = merged
        self._register(item, idx)
        self._register(merged, idx)
        return False

    def aliases(self) -> Dict[str, str]:
        """Every merged-away link -> the link of the record kept for its paper."""
        return {alt: r["link"] for r in self.records if r.get("link") for alt in r.get("alt_links", [])}


def split_seen(items: List[dict], index: "PaperIndex") -> tuple:
    """(items not yet in `index`, a crosswalk row for each one that is). A later search that
    finds a paper again drops it, so it is judged once; the row keeps how it was found again
    (link, source, query/rank or seed) and the link of the record it matched."""
    new, seen = [], []
    for it in items:
        idx = index.find(it)
        if idx is None:
            new.append(it)
        else:
            seen.append({**_route(it), "title": it.get("title"), "routes": it.get("routes"),
                         "matched_link": index.records[idx].get("link")})
    return new, seen


def remove_duplicates(items: list[dict], merge=None) -> list[dict]:
    """
    Removes duplicate paper records from a list: one record per paper, in first-seen order.
    Records are one paper when they share a URL, DOI, arXiv id or a specific title (see
    PaperIndex); `merge(kept, incoming)` combines them (default: prefer_record).

    Args:
        items: A list of paper records.
        merge: optional function combining two records of one paper.

    Returns:
        A deduplicated list of paper records.
    """
    return PaperIndex(items, merge=merge).records

def dedupe_merge(a: list[dict], b: list[dict]) -> list[dict]:
    """
    Merges two lists of paper records and removes duplicates.

    Args:
        a: The first list of paper records.
        b: The second list of paper records.

    Returns:
        A single, combined, and deduplicated list of paper records.
    """
    combined = a + b
    return remove_duplicates(combined)


def normalize_slug(slug: str) -> str:
    """
    Normalize a slug for deduplication.
    
    Args:
        slug: Raw slug string
        
    Returns:
        Normalized slug (lowercase, no special chars, collapsed separators)
    """
    if not slug:
        return ""
    
    # Convert to lowercase
    normalized = slug.lower().strip()
    
    # Replace multiple separators with single hyphen
    normalized = re.sub(r'[-_\s]+', '-', normalized)
    
    # Remove non-alphanumeric characters except hyphens
    normalized = re.sub(r'[^a-z0-9-]', '', normalized)
    
    # Remove leading/trailing hyphens
    normalized = normalized.strip('-')
    
    # Collapse multiple hyphens
    normalized = re.sub(r'-+', '-', normalized)
    
    return normalized
