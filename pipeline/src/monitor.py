"""Watch a running pipeline: is it still getting full text, and is any site refusing us?

A site that blocks the run's IP doesn't stop anything: the paper falls back to its
open-access copy or abstract, and the run carries on with less text, and fewer metrics
(88% of full-text papers gave metrics in the keyed smoke run, 48% of abstract-only ones).
This reads the run's usage log (WSI_USAGE_LOG; `cli papers` writes
pipeline/data/usage_log.jsonl) and the shared host state, and reports:

- where each fetched paper's text came from, per hour;
- per site over the last hour: requests, blocks, 429s, and the full-text share of its papers;
- sites cooling down now, and whether OpenAlex's daily credits are spent;
- LLM calls, errors, flex fallbacks, network outages;
- alerts: the full-text share falling against the run so far, a site whose full text
  stopped, title-only papers rising, or the log going quiet (a stalled run).

`cli monitor --watch 10` prints it every 10 minutes; `--notify` also raises a macOS
notification when a new alert appears.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List

from pipeline.utils.scraping.http import _state_dir, host_of

SOURCES = ("full_text", "abstract", "partial", "title_snippet")


def read_events(path: Path, since: float = 0.0) -> List[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("ts", 0) >= since:
                    out.append(rec)
    except OSError:
        pass
    return out


def _share(c: Counter, key: str = "full_text") -> float:
    n = sum(c.values())
    return c[key] / n if n else 0.0


def host_states() -> Dict[str, dict]:
    out = {}
    for p in sorted(_state_dir().glob("*.json")):
        try:
            out[p.stem] = json.loads(p.read_text(encoding="utf-8") or "{}")
        except ValueError:
            continue
    return out


def alerts(events: List[dict], now: float, window_s: float = 3600, min_papers: int = 60) -> List[str]:
    """The conditions worth a person's attention, from the run's events."""
    found = []
    fetches = [e for e in events if e.get("kind") == "fetch"]
    recent = [e for e in fetches if e["ts"] >= now - window_s]
    earlier = [e for e in fetches if e["ts"] < now - window_s]
    rc, ec = Counter(e.get("source") for e in recent), Counter(e.get("source") for e in earlier)
    if len(recent) >= min_papers and len(earlier) >= min_papers:
        drop = _share(ec) - _share(rc)
        if drop >= 0.15:
            found.append(f"full-text share fell to {_share(rc):.0%} in the last hour, from {_share(ec):.0%} "
                         f"before ({len(recent)} papers)")
    if len(recent) >= min_papers and _share(rc, "title_snippet") >= 0.15:
        found.append(f"{_share(rc, 'title_snippet'):.0%} of the last hour's papers had only title and snippet")
    by_host_recent: Dict[str, Counter] = defaultdict(Counter)
    by_host_earlier: Dict[str, Counter] = defaultdict(Counter)
    for e in recent:
        by_host_recent[host_of(e.get("link", ""))][e.get("source")] += 1
    for e in earlier:
        by_host_earlier[host_of(e.get("link", ""))][e.get("source")] += 1
    for host, c in by_host_recent.items():
        before = by_host_earlier.get(host)
        if before and sum(c.values()) >= 10 and sum(before.values()) >= 20 and _share(before) >= 0.5 \
                and _share(c) <= _share(before) - 0.4:
            found.append(f"{host}: full text fell to {_share(c):.0%} in the last hour, from {_share(before):.0%}")
    for host, s in host_states().items():
        if float(s.get("cool_until", 0)) > now:
            found.append(f"{host} is cooling down for {(float(s['cool_until']) - now) / 60:.0f} more min "
                         f"({s.get('cool_reason', '')})")
    if events and now - max(e.get("ts", 0) for e in events) > 900:
        found.append(f"no pipeline activity for {(now - max(e.get('ts', 0) for e in events)) / 60:.0f} min "
                     f"(stalled, finished, or waiting on a batch)")
    return found


def report(log: Path, window_min: float = 60, top_hosts: int = 12) -> str:
    now = time.time()
    events = read_events(log)
    if not events:
        return f"No events in {log} yet (set WSI_USAGE_LOG, or run `cli papers`, which writes it)."
    lines = [f"== {log}: {len(events)} events, {time.strftime('%Y-%m-%d %H:%M')}, "
             f"last {int(now - events[-1]['ts'])} s ago"]

    # Text source per hour.
    hours: Dict[int, Counter] = defaultdict(Counter)
    for e in events:
        if e.get("kind") == "fetch":
            hours[int(e["ts"] // 3600)][e.get("source")] += 1
    if hours:
        lines.append("\nWhere each paper's text came from, per hour:")
        lines.append(f"  {'hour':<12}{'papers':>7}" + "".join(f"{s:>15}" for s in SOURCES))
        total = Counter()
        for h in sorted(hours)[-24:]:
            c = hours[h]
            total += c
            n = sum(c.values())
            lines.append(f"  {time.strftime('%m-%d %H:00', time.localtime(h * 3600)):<12}{n:>7}"
                         + "".join(f"{c[s]:>8} ({c[s] / n:>4.0%})" for s in SOURCES))
        n = sum(total.values())
        lines.append(f"  {'shown':<12}{n:>7}" + "".join(f"{total[s]:>8} ({total[s] / n:>4.0%})" for s in SOURCES))

    # Sites over the last window.
    since = now - window_min * 60
    http = [e for e in events if e.get("kind") == "http" and e["ts"] >= since]
    fetch_src: Dict[str, Counter] = defaultdict(Counter)
    for e in events:
        if e.get("kind") == "fetch" and e["ts"] >= since:
            fetch_src[host_of(e.get("link", ""))][e.get("source")] += 1
    if http:
        per: Dict[str, Counter] = defaultdict(Counter)
        for e in http:
            st = e.get("status")
            per[e.get("host", "")]["n"] += 1
            per[e.get("host", "")]["ok" if st == 200 and not e.get("blocked") else
                                   "blocked" if e.get("blocked") or st in (401, 403) else
                                   "throttled" if st in (429, 503) else
                                   "skipped" if st == "cooling" else "other"] += 1
        lines.append(f"\nSites, last {window_min:.0f} min (requests; papers whose link is on the site):")
        lines.append(f"  {'host':<34}{'req':>6}{'ok':>6}{'blocked':>9}{'429/503':>9}{'skipped':>9}{'papers':>8}{'full text':>11}")
        for host, c in sorted(per.items(), key=lambda kv: -kv[1]["n"])[:top_hosts]:
            fs = fetch_src.get(host, Counter())
            n = sum(fs.values())
            lines.append(f"  {host[:33]:<34}{c['n']:>6}{c['ok']:>6}{c['blocked']:>9}{c['throttled']:>9}"
                         f"{c['skipped']:>9}{n:>8}{(f'{_share(fs):.0%}' if n else '-'):>11}")

    states = host_states()
    cooling = {h: s for h, s in states.items() if float(s.get("cool_until", 0)) > now}
    if cooling:
        lines.append("\nCooling down now:")
        for h, s in sorted(cooling.items(), key=lambda kv: -float(kv[1]["cool_until"])):
            lines.append(f"  {h:<34} {(float(s['cool_until']) - now) / 60:>5.0f} min  {s.get('cool_reason', '')}")
    oa = states.get("api.openalex.org", {})
    if float(oa.get("search_blocked_until", 0)) > now:
        lines.append(f"\nOpenAlex: credits spent; title searches and PDF downloads off for "
                     f"{(float(oa['search_blocked_until']) - now) / 3600:.1f} h more (DOI lookups continue)")
    elif float(oa.get("content_blocked_until", 0)) > now:
        lines.append(f"\nOpenAlex: too few credits for PDF downloads for "
                     f"{(float(oa['content_blocked_until']) - now) / 3600:.1f} h more (title searches continue)")
    copies = [e for e in events if e.get("kind") == "openalex_content"]
    upgrades = [e for e in events if e.get("kind") == "fetch_upgrade"]
    if copies:
        lines.append(f"OpenAlex PDF copies in the run: {len(copies)} downloaded (${len(copies) / 100:.2f}), "
                     f"{len(upgrades)} kept papers moved to full text")

    llm = [e for e in events if e.get("kind") == "llm" and e["ts"] >= since]
    errs = [e for e in events if e.get("kind") == "llm_error" and e["ts"] >= since]
    flex = [e for e in events if e.get("kind") == "flex_fallback" and e["ts"] >= since]
    outages = [e for e in events if e.get("kind") == "outage"]
    lines.append(f"\nLLM, last {window_min:.0f} min: {len(llm)} calls, {len(errs)} failed for good, "
                 f"{len(flex)} sent at the default tier (flex busy); network outages in the run: {len(outages)}")

    found = alerts(events, now)
    lines.append("\nAlerts:" if found else "\nAlerts: none")
    lines.extend(f"  ! {a}" for a in found)
    return "\n".join(lines)


def notify(message: str) -> None:
    """A macOS notification; elsewhere, the terminal bell and the message."""
    import subprocess
    if sys.platform == "darwin":
        script = f'display notification {json.dumps(message[:200])} with title "WSI Hub run"'
        try:
            subprocess.run(["osascript", "-e", script], capture_output=True)
            return
        except OSError:
            pass
    print(f"\a\n*** WSI Hub run: {message[:200]} ***", flush=True)
