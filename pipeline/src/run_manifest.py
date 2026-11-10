"""One run per data root, and a manifest that says what produced it.

Every stage loads its output when the file is already there, so a run started in a data
root that holds another run's files silently carries them forward (and the batch cache is
keyed by step name). `begin_run` refuses that: a data root with outputs is only reused by
resuming the run that wrote it (`--run-id <its id>`). A new run needs an empty root -- a
fresh checkout or worktree.

The manifest (pipeline/data/run_manifest.json) records what can't be reconstructed after
the run: the command, commit and uncommitted diff, the WSI_* settings, the hashes of every
prompt, schema and list under pipeline/config, every installed package's version, and each
start/resume and its end status. Each session also saves the code it ran as a patch on the
commit plus its untracked files (code_snapshot/), and reads the SerpAPI account (searches
left) at its start and end; with OPENAI_ADMIN_KEY set it reads the organization's OpenAI
costs too. `cli note` adds what only a person knows: provider balances read off a
dashboard, the time spent stepping in.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import click
import requests
from filelock import FileLock

MANIFEST = "run_manifest.json"
# Directories whose files a later stage would load instead of recomputing.
STAGE_DIRS = ("paper_data", "dataset_data", "batch_state", "batch_results", "batch_inputs", "batch_outputs",
              "checkpoints")
CONFIG = Path("pipeline/config")
PACKAGES = ("openai", "google-genai", "requests", "jsonschema", "tiktoken", "click")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True, encoding="utf-8").stdout
    except Exception:
        return ""


def stage_outputs(data: Path) -> List[Path]:
    """Files in `data` that a stage would pick up as already done."""
    return sorted(p for d in STAGE_DIRS if (data / d).is_dir() for p in (data / d).rglob("*") if p.is_file()
                  and p.name not in (".gitkeep", "README.md"))


def _package_versions() -> dict:
    from importlib import metadata
    out = {}
    for name in PACKAGES:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    return out


def _all_packages() -> dict:
    """Every installed distribution, as `pip freeze` would list it."""
    from importlib import metadata
    return dict(sorted((d.metadata["Name"], d.version) for d in metadata.distributions() if d.metadata["Name"]))


def snapshot_code(dest: Path) -> dict:
    """The code as it ran: `git diff HEAD` and the untracked files under pipeline/ and
    evaluation/, so a dirty checkout can be rebuilt. Returns their paths and hashes."""
    dest.mkdir(parents=True, exist_ok=True)
    diff = _git("diff", "HEAD", "--binary")
    out = {}
    if diff:
        (dest / "uncommitted.diff").write_text(diff, encoding="utf-8")
        out["diff"] = {"path": (dest / "uncommitted.diff").as_posix(), "sha256": _sha256(diff.encode())}
    untracked = [f for f in _git("ls-files", "--others", "--exclude-standard", "pipeline", "evaluation").splitlines()
                 if f and Path(f).is_file()]
    if untracked:
        import zipfile
        with zipfile.ZipFile(dest / "untracked.zip", "w", zipfile.ZIP_DEFLATED) as z:
            for f in untracked:
                z.write(f)
        out["untracked"] = {"path": (dest / "untracked.zip").as_posix(), "files": untracked}
    return out


def accounts() -> dict:
    """Provider balances now, where an API gives them. Failures are recorded, not raised."""
    out = {"at": _now()}
    key = os.getenv("SERPAPI_API_KEY")
    if key:
        try:  # free: the account endpoint is not billed as a search
            r = requests.get("https://serpapi.com/account.json", params={"api_key": key}, timeout=20)
            a = r.json() if r.ok else {}
            if not r.ok or a.get("error"):
                out["serpapi"] = {"error": f"HTTP {r.status_code}: {a.get('error') or r.text[:200]}"}
            else:
                out["serpapi"] = {k: a.get(k) for k in ("plan_name", "searches_per_month", "plan_searches_left",
                                                         "extra_credits", "total_searches_left", "this_month_usage",
                                                         "last_hour_searches")}
        except Exception as e:  # noqa: BLE001
            out["serpapi"] = {"error": f"{type(e).__name__}: {e}"[:200]}
    return out


def openai_costs(start_iso: str) -> dict:
    """The organization's OpenAI costs since `start_iso` (daily buckets, so the first and
    last day include whatever else the organization ran, and the last day may not be final
    yet). Needs OPENAI_ADMIN_KEY. A refused request is recorded as an error, never as $0."""
    key = os.getenv("OPENAI_ADMIN_KEY")
    if not key:
        return {}
    try:
        start = int(datetime.fromisoformat(start_iso).replace(hour=0, minute=0, second=0).timestamp())
        total, page, buckets = 0.0, None, []
        while True:
            params = {"start_time": start, "limit": 31, **({"page": page} if page else {})}
            resp = requests.get("https://api.openai.com/v1/organization/costs", params=params,
                                headers={"Authorization": f"Bearer {key}"}, timeout=30)
            if not resp.ok:
                return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
            r = resp.json()
            for b in r.get("data", []):
                usd = sum(float(x.get("amount", {}).get("value") or 0) for x in b.get("results", []))
                buckets.append({"start": b.get("start_time"), "usd": round(usd, 4)})
                total += usd
            page = r.get("next_page")
            if not r.get("has_more") or not page:
                break
        return {"since": start_iso, "scope": "organization, daily buckets", "usd": round(total, 4),
                "daily": buckets}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:200]}


def describe_code() -> dict:
    diff = _git("diff", "HEAD")
    return {
        "commit": _git("rev-parse", "HEAD").strip(),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD").strip(),
        "dirty_files": [ln[3:] for ln in _git("status", "--porcelain").splitlines()],
        "diff_sha256": _sha256(diff.encode()) if diff else "",
        "config_sha256": {p.as_posix(): _sha256(p.read_bytes()) for p in sorted(CONFIG.rglob("*")) if p.is_file()},
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "packages": _package_versions(),
    }


def begin_run(data: Path, run_id: Optional[str], options: dict) -> dict:
    """Start or resume the run that owns `data`; refuse a root holding another run's files."""
    path = data / MANIFEST
    data.mkdir(parents=True, exist_ok=True)
    with _locked(path):
        return _begin_run(data, path, run_id, options)


def _locked(path: Path) -> FileLock:
    """Every change to the manifest reads, updates and replaces it under this lock: `cli
    note` runs beside the pipeline, and two writers each rewriting the whole file lose
    each other's changes."""
    return FileLock(str(path) + ".lock", timeout=60)


def _write(path: Path, manifest: dict) -> None:
    from pipeline.src.utils import atomic_json
    atomic_json(path, manifest, indent=1)


def _begin_run(data: Path, path: Path, run_id: Optional[str], options: dict) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    outputs = stage_outputs(data)
    if outputs:
        owner = (manifest or {}).get("run_id")
        if not owner or owner != run_id:
            raise click.ClickException(
                f"{data} already holds {len(outputs)} stage outputs (e.g. {outputs[0]}) from "
                + (f"run {owner!r}" if owner else "a run with no manifest")
                + ". Every stage would reuse them. Start a new run in an empty data root (a fresh "
                  "checkout or worktree)"
                + (f", or resume that run with --run-id {owner}." if owner else "."))
    if manifest is None or manifest.get("run_id") != run_id:
        manifest = {"run_id": run_id or datetime.now().strftime("run_%Y%m%d_%H%M%S"), "sessions": []}
    session = len(manifest["sessions"]) + 1
    manifest["sessions"].append({
        "started_at": _now(), "argv": sys.argv, "options": options,
        "env": {k: v for k, v in sorted(os.environ.items()) if k.startswith("WSI_")},
        "openai_service_tier": os.getenv("WSI_OPENAI_SERVICE_TIER", "flex") or "default",
        "code": describe_code(), "code_snapshot": snapshot_code(data / "code_snapshot" / f"session_{session}"),
        "packages_all": _all_packages(), "accounts_start": accounts(), "status": "running"})
    _write(path, manifest)
    click.echo(f"Run {manifest['run_id']} (session {len(manifest['sessions'])}) -> {path}")
    return manifest


def end_run(data: Path, status: str, error: str = "") -> None:
    path = data / MANIFEST
    if not path.exists():
        return
    from pipeline.src.provenance import verify_run
    started = json.loads(path.read_text(encoding="utf-8"))["sessions"][0]["started_at"]
    # The provider calls and the archive scan first, outside the lock; then one locked
    # read-update-write.
    costs, balances, ended, check = openai_costs(started), accounts(), _now(), verify_run(data, deep=True)
    if not check["complete"]:
        click.echo("WARNING: the run's evidence is incomplete (run_manifest.json: archive_check; "
                   "`cli verify-run` for details)", err=True)
    with _locked(path):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["sessions"][-1].update({
            "ended_at": ended, "status": status, "accounts_end": balances,
            # does the archive hold everything the records point at (provenance.verify_run)
            "archive_check": check,
            **({"openai_costs_since_run_start": costs} if costs else {}),
            **({"error": error} if error else {})})
        _write(path, manifest)


def add_note(data: Path, text: str, minutes: Optional[float] = None, **values) -> dict:
    """Append a dated note to the run's manifest: a balance read off a dashboard, or time
    spent stepping in (minutes) and why."""
    path = data / MANIFEST
    if not path.exists():
        raise click.ClickException(f"No run manifest at {path}")
    note = {"at": _now(), "text": text, **({"minutes": minutes} if minutes is not None else {}),
            **{k: v for k, v in values.items() if v is not None}}
    with _locked(path):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest.setdefault("notes", []).append(note)
        _write(path, manifest)
    return note
