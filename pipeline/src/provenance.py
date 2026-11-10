"""What a run saw, kept so the run can be audited after it ends.

A stage's output holds what it decided, not what it decided on: the gate dropped a paper
and kept nothing of the text it judged; a search kept the parsed results, not SerpAPI's
answer. Refetching later gets different pages. With WSI_ARCHIVE_DIR set (`cli papers` sets
pipeline/data/archive), the stages archive those inputs here, content-addressed and
gzipped: `<kind>/<sha[:2]>/<sha256>.gz`, written once. Records elsewhere refer to them by
hash. Unset (the scorers, tests), nothing is written and the hashes are still returned.

The archive holds copyrighted text, like the rest of pipeline/data: keep it private.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from pipeline.src.utils import replace_retry


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _store(kind: str, sha: str, data: bytes) -> bool:
    """Write once; True when the object is stored (or no archive is kept). Never raises: a
    failed archive write must not fail the call it records."""
    root = os.getenv("WSI_ARCHIVE_DIR")
    if not root:
        return True
    path = Path(root) / kind / sha[:2] / f"{sha}.gz"
    if path.exists():
        return True
    # Threads of one process share a pid: two of them archiving the same prompt at once
    # collided on a pid-named temp file (Windows: Permission denied).
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    error = None
    for wait in (0, 0.5, 2, 5):  # a scanner or another process holding the file lets go
        time.sleep(wait)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(gzip.compress(data))
            replace_retry(tmp, path)
            return True
        except OSError as e:  # another worker wrote the same content first, or the disk refused
            error = e
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            if path.exists():
                return True
    # Really lost. Recorded here when the disk still takes that; verify_run() finds it either way.
    from pipeline.utils.llms.llm import _append
    if not _append(str(Path(root) / FAILURES), json.dumps({"kind": kind, "sha256": sha,
                                                           "error": f"{type(error).__name__}: {error}"[:200]}) + "\n"):
        logging.getLogger(__name__).error("archive: could not store %s/%s (%s)", kind, sha, error)
    return False


def persist_response(record: dict) -> bool:
    """One answered LLM call (its ledger line: call_id, raw answer, usage, prompt hashes) as
    an archive object of its own, beside the ledger. Two independent copies: verify_run
    flags a response whose ledger line is missing. True when stored, or no archive is kept."""
    data = json.dumps(record, sort_keys=True, default=str).encode("utf-8")
    return _store("llm_response", hashlib.sha256(data).hexdigest(), data)


FAILURES = "_write_failures.jsonl"


def write_failures(root: str | None = None) -> int:
    """How many archive writes were recorded as lost (verify_run is the full check)."""
    return len(_failed_writes(Path(root or os.getenv("WSI_ARCHIVE_DIR") or "pipeline/data/archive")))


def _failed_writes(root: Path) -> list:
    path = root / FAILURES
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []


EMPTY = sha256_text("")


class _Lines:
    """A JSONL file's records, counting the lines that don't parse (a cut-off write)."""

    def __init__(self):
        self.malformed = {}

    def read(self, path: Path):
        if not path.exists():
            return
        bad = 0
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except ValueError:
                bad += 1
        if bad:  # a file read twice counts once
            self.malformed[path.name] = max(bad, self.malformed.get(path.name, 0))


def _referenced(data: Path, lines: _Lines):
    """(kind, sha256) for every archived object a run's records point at."""
    for f in sorted((data / "paper_data").glob("*_decisions.json")):
        for row in json.loads(f.read_text(encoding="utf-8")):
            sha = (row.get("gate") or {}).get("text_sha256")
            if sha:
                yield "gate_text", sha
    for f in sorted((data / "paper_data").glob("*_queries.json")):
        for entry in json.loads(f.read_text(encoding="utf-8")):
            for page in entry.get("pages") or []:
                for sha in page.get("attempt_sha256") or [page.get("raw_sha256")]:
                    if sha:
                        yield "serpapi", sha
    for row in lines.read(data / "llm_calls.jsonl"):
        for kind, sha in (("llm_prompt", row.get("system_sha256")), ("llm_input", row.get("user_sha256"))):
            if sha and sha != EMPTY:
                yield kind, sha
        for a in row.get("attachments") or []:
            yield "llm_attachment", a.get("sha256")
    for row in lines.read(data / "usage_log.jsonl"):
        if row.get("error_sha256") and row["error_sha256"] != EMPTY:
            yield "serpapi_error", row["error_sha256"]
    for index in sorted((data / "dataset_data").glob("**/pages_index.jsonl")):
        for row in lines.read(index):
            if row.get("sha256"):
                yield "dataset_page", row["sha256"]


def _intact(path: Path, sha: str) -> bool:
    try:
        return hashlib.sha256(gzip.decompress(path.read_bytes())).hexdigest() == sha
    except (OSError, EOFError, gzip.BadGzipFile):
        return False


def verify_run(data: Path, deep: bool = False) -> dict:
    """Is the run's evidence all there? `complete` only when:
    - every archived object a record points at exists (with `deep`, also decompresses to
      its hash);
    - the LLM ledger and the usage log hold the same answered calls, matched by call_id
      (not by count: one lost line in each would cancel out), and every call's own response
      record (persist_response) has its ledger line;
    - no archived LLM input or prompt is unreferenced: the ledger line that pointed at it
      was lost;
    - every write recorded as failed was later stored after all;
    - no JSONL record is cut off.
    A run whose records point at missing evidence is not completely archived, whatever
    _write_failures.jsonl says, and a clean _write_failures.jsonl proves nothing (the disk
    that refused the archive can refuse the log too)."""
    root = data / "archive"
    lines = _Lines()
    refs = set(_referenced(data, lines))
    blob = lambda k, s: root / k / s[:2] / f"{s}.gz"  # noqa: E731
    missing = sorted((k, s) for k, s in refs if not (_intact(blob(k, s), s) if deep else blob(k, s).exists()))
    by_kind = {}
    for k, _ in refs:
        by_kind.setdefault(k, [0, 0])[0] += 1
    for k, _ in missing:
        by_kind[k][1] += 1

    # Archived LLM inputs no ledger line points at: their ledger lines were lost.
    unreferenced = sorted(f"{k}/{f.name[:-3]}" for k in ("llm_prompt", "llm_input", "llm_attachment")
                          for f in (root / k).glob("*/*.gz") if (k, f.name[:-3]) not in refs)
    ledger = {r["call_id"] for r in lines.read(data / "llm_calls.jsonl") if r.get("call_id") and "raw" in r}
    usage = {r["call_id"] for r in lines.read(data / "usage_log.jsonl") if r.get("kind") == "llm" and r.get("call_id")}
    unresolved = [f for f in _failed_writes(root) if not blob(f.get("kind", ""), f.get("sha256", "")).exists()]
    # Each answered call's own record (persist_response): one whose ledger line is gone.
    responses = set()
    for f in (root / "llm_response").glob("*/*.gz"):
        try:
            responses.add(json.loads(gzip.decompress(f.read_bytes())).get("call_id"))
        except (OSError, EOFError, ValueError):
            lines.malformed[f"llm_response/{f.name}"] = 1
    responses.discard(None)
    problems = {
        "missing_objects": len(missing),
        "calls_missing_from_ledger": len(usage - ledger),
        "calls_missing_from_usage_log": len(ledger - usage),
        "responses_missing_from_ledger": len(responses - ledger),
        "unreferenced_llm_objects": len(unreferenced),
        "unresolved_write_failures": len(unresolved),
        "malformed_lines": sum(lines.malformed.values()),
    }
    return {"complete": not any(problems.values()), **problems,
            "referenced": len(refs), "deep": deep,
            "by_kind": {k: {"referenced": n, "missing": m} for k, (n, m) in sorted(by_kind.items())},
            "ledger_calls": len(ledger), "usage_log_calls": len(usage),
            "malformed_by_file": lines.malformed,
            "examples": {"missing": [f"{k}/{s}" for k, s in missing[:10]], "unreferenced": unreferenced[:10],
                         "missing_from_ledger": sorted(usage - ledger)[:10],
                         "missing_from_usage_log": sorted(ledger - usage)[:10]}}


def archive_text(kind: str, text: str) -> str:
    """Archive `text` under `kind`; return its sha256."""
    sha = sha256_text(text)
    if text:
        _store(kind, sha, text.encode("utf-8"))
    return sha


def archive_bytes(kind: str, data: bytes) -> str:
    """Archive raw bytes (a PDF sent with a call) under `kind`; return their sha256."""
    sha = hashlib.sha256(data or b"").hexdigest()
    if data:
        _store(kind, sha, data)
    return sha


def archive_json(kind: str, obj: Any) -> str:
    """Archive a JSON-able object under `kind`; return the sha256 of its serialization."""
    data = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    sha = hashlib.sha256(data).hexdigest()
    _store(kind, sha, data)
    return sha


def read_archived(kind: str, sha: str, root: str | None = None) -> bytes | None:
    path = Path(root or os.getenv("WSI_ARCHIVE_DIR") or "pipeline/data/archive") / kind / sha[:2] / f"{sha}.gz"
    return gzip.decompress(path.read_bytes()) if path.exists() else None
