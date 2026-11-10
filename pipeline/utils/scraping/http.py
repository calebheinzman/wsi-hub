"""Every outbound request the pipeline makes, through one function: `polite_get`.

A full run fetches 25-40 papers a minute for days from one IP, and each paper can touch
the publisher, OpenAlex, Crossref, Semantic Scholar, Europe PMC and a repository. What
this adds over a bare `requests.get`:

- **Pacing per site, across processes.** Requests to one host start at least
  `interval(host)` seconds apart, through a lock file per host under WSI_HOST_STATE_DIR,
  so the gate's process pool and the extraction threads share one schedule. APIs get the
  rate they publish; publishers get WSI_HOST_INTERVAL.
- **Backing off when a site asks.** A 429 / 503 with a short Retry-After is retried after
  it; a longer one, or one without, cools the host down: its requests are skipped (raised
  as `HostCooling`) until the time is up, so papers on it go straight to the open-access
  fallbacks instead of hammering a site that is refusing us.
- **Noticing blocks.** A 403 or a CAPTCHA / bot-check page counts against the host; when
  most of its last 10 answers were blocks it cools down too, starting at 10 minutes and
  doubling to 4 hours. Every success resets the backoff.
- **Waiting out a dropped connection.** A connection error while the machine is offline
  waits for the network (up to WSI_OUTAGE_WAIT seconds) and retries, so an outage does not
  turn into thousands of papers judged on their titles.
- **A total deadline.** `timeout` bounds each read; WSI_FETCH_DEADLINE bounds the whole
  download, so a server dribbling bytes can't hold a worker for an hour.

Each request goes to the WSI_USAGE_LOG as an "http" event (host, status, seconds, and
whether it was blocked, retried or skipped), which `cli monitor` reads.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import requests

from pipeline.utils.llms.llm import log_event

if os.name == "nt":
    from filelock import FileLock
else:
    import fcntl


class HostCooling(requests.RequestException):
    """The host is cooling down after refusing us; the request was not sent."""


def contact_email() -> str:
    """WSI_CONTACT_EMAIL (or OPENALEX_MAILTO): sent to the APIs that ask for one."""
    return os.getenv("WSI_CONTACT_EMAIL") or os.getenv("OPENALEX_MAILTO") or ""


def host_of(url: str) -> str:
    host = (urlparse(url or "").netloc or "").lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


# APIs: their own error handling decides what a 429 means (OpenAlex: the daily budget;
# Semantic Scholar: erratic load), so they are paced but never cooled or block-checked.
API_HOSTS = {"api.openalex.org", "content.openalex.org", "api.crossref.org", "api.semanticscholar.org", "ebi.ac.uk",
             "api.unpaywall.org", "serpapi.com", "google.serper.dev", "api.perplexity.ai", "r.jina.ai"}


def interval(host: str) -> float:
    """Minimum seconds between two requests to `host`."""
    if host == "api.semanticscholar.org":
        return float(os.getenv("WSI_S2_INTERVAL", "1.1"))  # 1 request/s per key, all endpoints
    if host == "api.crossref.org":
        # Public pool: 1 request/s, 1 at a time; with a contact email, the polite pool.
        return 0.25 if contact_email() else 1.1
    fixed = {"api.openalex.org": 0.12, "content.openalex.org": 0.12, "ebi.ac.uk": 0.2, "api.unpaywall.org": 0.1,
             "r.jina.ai": 1.5, "arxiv.org": 3.0, "export.arxiv.org": 3.0,
             "serpapi.com": 0.0, "google.serper.dev": 0.0, "api.perplexity.ai": 0.0}
    if host in fixed:
        return fixed[host]
    return float(os.getenv("WSI_HOST_INTERVAL", "2.0"))


def _state_dir() -> Path:
    default = Path(tempfile.gettempdir()) / "wsi_hosts" if os.name == "nt" else "/tmp/wsi_hosts"
    d = Path(os.getenv("WSI_HOST_STATE_DIR", default))
    d.mkdir(parents=True, exist_ok=True)
    return d


def with_host_state(host: str, fn: Callable[[dict], object]):
    """Run fn on the host's shared state (a JSON dict) under its lock; fn may mutate it."""
    path = _state_dir() / f"{host or '_'}.json"
    if os.name == "nt":
        # No flock on Windows, and its locks are mandatory: locking the JSON itself would
        # stop `cli monitor` reading it, so the lock is a file beside it.
        with FileLock(path.with_suffix(".lock"), poll_interval=0.01):
            with open(path, "a+", encoding="utf-8") as f:
                return _update_state(f, fn)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            return _update_state(f, fn)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _update_state(f, fn: Callable[[dict], object]):
    f.seek(0)
    try:
        state = json.loads(f.read() or "{}")
    except ValueError:
        state = {}
    before = json.dumps(state, sort_keys=True)
    out = fn(state)
    if json.dumps(state, sort_keys=True) != before:
        f.seek(0)
        f.truncate()
        f.write(json.dumps(state))
        f.flush()
    return out


def host_state(host: str) -> dict:
    return with_host_state(host, lambda s: dict(s))


def _wait_turn(host: str) -> None:
    gap = interval(host)
    if gap <= 0:
        return

    def reserve(s):
        slot = max(time.time(), float(s.get("next", 0)))
        s["next"] = slot + gap
        return slot

    wait = with_host_state(host, reserve) - time.time()
    if wait > 0:
        time.sleep(wait)


def cooling_until(host: str) -> float:
    return float(host_state(host).get("cool_until", 0))


def _cool(host: str, seconds: float, reason: str) -> None:
    def f(s):
        s["cool_until"] = max(float(s.get("cool_until", 0)), time.time() + seconds)
        s["cool_reason"] = reason
        s["cools"] = int(s.get("cools", 0)) + 1
    with_host_state(host, f)


_STRIKE_WINDOW = 10


def _record_outcome(host: str, blocked: bool) -> Optional[float]:
    """Add one answer to the host's window; cool it when most recent answers were blocks.
    Returns the cool-down started, if any."""
    need = int(os.getenv("WSI_HOST_STRIKES", "8"))

    def f(s):
        window = (s.get("window", "") + ("1" if blocked else "0"))[-_STRIKE_WINDOW:]
        s["window"] = window
        if not blocked:
            s["backoff"] = 0
            return None
        if window.count("1") >= need and time.time() >= float(s.get("cool_until", 0)):
            backoff = min(max(float(s.get("backoff", 0)) * 2, 600.0), 14400.0)
            s["backoff"] = backoff
            s["cool_until"] = time.time() + backoff
            s["cool_reason"] = f"{window.count('1')} of the last {len(window)} answers were blocks"
            s["cools"] = int(s.get("cools", 0)) + 1
            return backoff
        return None

    return with_host_state(host, f)


# Bot checks and block pages. Only short pages are checked: an article page is far longer,
# and a short page is useless as paper text either way.
_BLOCK_MARKERS = (
    "just a moment...", "cf-chl-", "attention required! | cloudflare", "cf-browser-verification",
    "captcha", "are you a robot", "unusual traffic", "access denied", "request unsuccessful. incapsula",
    "pardon our interruption", "please enable cookies", "bot verification", "px-captcha",
    "verify you are human", "checking your browser",
)
_BLOCK_PAGE_MAX_BYTES = 25_000


def looks_blocked(resp) -> bool:
    # IEEE answers scrapers 418; AWS WAF challenges answer 202 with an empty body.
    body = getattr(resp, "content", b"") or b""
    if resp.status_code in (401, 403, 418) or (resp.status_code == 202 and not body):
        return True
    if resp.status_code != 200:
        return False
    if len(body) > _BLOCK_PAGE_MAX_BYTES or body[:5] == b"%PDF-":
        return False
    text = body.decode("utf-8", "ignore").lower()
    return any(m in text for m in _BLOCK_MARKERS)


def online() -> bool:
    """Whether the machine can reach the internet at all (not whether one host is up):
    direct connections to public resolvers, else an HTTPS request through any configured
    proxy (a network that blocks direct connections must not look offline for hours)."""
    for addr in (("1.1.1.1", 443), ("8.8.8.8", 53), ("208.67.222.222", 443)):
        try:
            socket.create_connection(addr, timeout=3).close()
            return True
        except OSError:
            continue
    for probe in ("https://www.apple.com/library/test/success.html", "https://www.google.com/generate_204"):
        try:
            requests.head(probe, timeout=5)
            return True
        except requests.RequestException:
            continue
    return False


def wait_for_network(what: str = "") -> bool:
    """Block until the machine is online again (True), or WSI_OUTAGE_WAIT seconds pass (False)."""
    if online():
        return True
    limit = float(os.getenv("WSI_OUTAGE_WAIT", "21600"))
    t0 = time.time()
    log_event("outage", what=what)
    print(f"[network] offline{f' ({what})' if what else ''}; waiting up to {limit / 3600:.1f} h for it to return",
          flush=True)
    while time.time() - t0 < limit:
        time.sleep(30)
        if online():
            log_event("outage_over", what=what, seconds=round(time.time() - t0))
            print(f"[network] back after {(time.time() - t0) / 60:.1f} min", flush=True)
            return True
    return False


def _read_body(resp, deadline: float, max_bytes: int) -> None:
    """Read a streamed body into resp._content. A watchdog closes the connection at the
    deadline: checking the clock between chunks doesn't stop a server that sends a few
    bytes every 20 s, since each read waits for its whole chunk. Too large raises."""
    if not hasattr(resp, "iter_content") or getattr(resp, "_content", False) is not False:
        return  # already read (or a test double)
    import threading
    expired = threading.Event()

    def stop():
        expired.set()
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass

    timer = threading.Timer(max(0.0, deadline - time.time()), stop)
    timer.daemon = True
    timer.start()
    chunks, size = [], 0
    try:
        for chunk in resp.iter_content(65536):
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                resp.close()
                raise requests.RequestException(f"{resp.url} is over {max_bytes // 1_000_000} MB")
    except requests.RequestException:
        raise
    except Exception as exc:  # noqa: BLE001 - the watchdog closing the socket mid-read
        if expired.is_set():
            raise requests.Timeout(f"download exceeded WSI_FETCH_DEADLINE for {resp.url}") from exc
        raise requests.ConnectionError(str(exc)) from exc
    finally:
        timer.cancel()
    if expired.is_set():
        raise requests.Timeout(f"download exceeded WSI_FETCH_DEADLINE for {resp.url}")
    resp._content = b"".join(chunks)
    resp._content_consumed = True


def _default_headers(api: bool) -> dict:
    """requests' User-Agent, as before. The contact email goes to the APIs only, which ask
    for it; publishers see the same User-Agent as in the earlier runs."""
    email = contact_email()
    ua = requests.utils.default_user_agent()
    return {"User-Agent": f"{ua} (+mailto:{email})" if email and api else ua}


# Redirectors: a doi.org link lands on whichever publisher holds the paper, so blocks are
# charged to the host the request ends on, and the redirector itself is never cooled.
REDIRECTORS = {"doi.org", "dx.doi.org"}


def polite_get(url: str, *, params: Optional[dict] = None, headers: Optional[dict] = None,
               timeout: float = 30, max_bytes: int = 60_000_000, method: str = "GET",
               json_body: Optional[dict] = None):
    """`requests.get` (or POST with `json_body`) with the pacing, backoff, block detection and
    outage handling described above. Raises HostCooling when the host is cooling down, and
    requests' own exceptions on failure. A blocked answer is returned with `resp.blocked`
    set, so callers treat it as no text."""
    host = host_of(url)
    api = host in API_HOSTS
    until = 0.0 if api or host in REDIRECTORS else cooling_until(host)
    if until > time.time():
        log_event("http", host=host, status="cooling", seconds=0)
        raise HostCooling(f"{host} is cooling down for {until - time.time():.0f}s more")
    hdrs = {**_default_headers(api), **(headers or {})}
    max_wait = float(os.getenv("WSI_MAX_RETRY_AFTER", "30"))
    deadline_s = float(os.getenv("WSI_FETCH_DEADLINE", "120"))
    network_retry = True
    for attempt in range(4):
        _wait_turn(host)
        t0 = time.time()
        try:
            kwargs = {"params": params, "headers": hdrs, "timeout": timeout, "stream": True}
            if method == "POST":
                resp = requests.post(url, json=json_body, **kwargs)
            else:
                resp = requests.get(url, **kwargs)
            _read_body(resp, t0 + deadline_s, max_bytes)
        except (requests.ConnectionError, requests.Timeout) as exc:
            seconds = round(time.time() - t0, 2)
            if network_retry and not online():
                network_retry = False
                log_event("http", host=host, status="offline", seconds=seconds)
                if wait_for_network(host):
                    continue
            # A failed DNS lookup or a reset connection while the machine is online is
            # usually momentary (the smoke check lost a Springer paper to one): once more.
            if isinstance(exc, requests.ConnectionError) and not isinstance(exc, requests.Timeout) \
                    and attempt == 0:
                log_event("http", host=host, status="ConnectionError-retry", seconds=seconds)
                time.sleep(3)
                continue
            log_event("http", host=host, status=type(exc).__name__, seconds=seconds)
            if not api and host not in REDIRECTORS:
                _record_outcome(host, blocked=isinstance(exc, requests.Timeout))
            raise
        except requests.RequestException as exc:  # a transfer broken off midway, a body too large
            log_event("http", host=host, status=type(exc).__name__, seconds=round(time.time() - t0, 2))
            raise
        seconds = round(time.time() - t0, 2)
        status = resp.status_code
        # Charge what follows to the host the request ended on (after redirects).
        landed = host_of(getattr(resp, "url", "") or "") or host
        if landed != host and not api:
            api = landed in API_HOSTS
            host = landed
        if host in REDIRECTORS:
            resp.blocked = False
            log_event("http", host=host, status=status, seconds=seconds)
            return resp
        if status in (429, 503) and not api:
            retry_after = (resp.headers.get("Retry-After") or "").strip()
            wait = float(retry_after) if retry_after.isdigit() else None
            if wait is not None and wait <= max_wait and attempt < 2:
                log_event("http", host=host, status=status, seconds=seconds, retry_after=wait)
                time.sleep(wait)
                continue
            cool = min(max(wait or 120.0, 60.0), 14400.0)
            _cool(host, cool, f"HTTP {status}" + (f", Retry-After {retry_after}" if retry_after else ""))
            log_event("http", host=host, status=status, seconds=seconds, cooled=cool)
            resp.blocked = True
            return resp
        blocked = False if api else looks_blocked(resp)
        resp.blocked = blocked
        cooled = None if api else _record_outcome(host, blocked)
        log_event("http", host=host, status=status, seconds=seconds, bytes=len(resp.content or b""),
                  **({"blocked": True} if blocked else {}), **({"cooled": cooled} if cooled else {}))
        return resp
    raise requests.ConnectionError(f"no answer from {host} after waiting for the network")


def interleave_by_host(items: list, link: Callable[[dict], str]) -> list:
    """`items` reordered round-robin across hosts, so consecutive work goes to different
    sites. Scholar returns results clustered by publisher; in input order a process pool
    sends a burst of requests to one site, which then queue behind its pacing."""
    from collections import deque
    groups: dict = {}
    for it in items:
        groups.setdefault(host_of(link(it)), deque()).append(it)
    queues = sorted(groups.values(), key=len, reverse=True)
    out = []
    while queues:
        for q in queues:
            out.append(q.popleft())
        queues = [q for q in queues if q]
    return out
