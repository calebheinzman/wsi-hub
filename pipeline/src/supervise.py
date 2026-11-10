"""Keep a long `papers` run going: when it stops on an error, resume it.

`cli papers` resumes from its stage outputs and checkpoints, but only when someone starts
it again. `cli keep-going` does that. It runs `papers` in a child process; when the child
exits with an error -- an exception no stage absorbed, a killed process, the network gone
for longer than WSI_OUTAGE_WAIT -- it waits and starts it again with `--run-id`, which
picks up at the step and item where it stopped.

It stops when the run completes, on Ctrl-C, after --max-restarts, when the run refuses to
start (the data root holds another run), or when the same error ends three sessions in a
row: that is a bug, and restarting would only pay for it again. StageIncomplete (a step
whose items kept failing -- an API outage, spent credits) is not a bug and never counts
toward that rule; it is resumed until --max-restarts.

On Ctrl-C the child gets the interrupt too, and is waited for rather than killed, so it
finishes writing what it was writing. (On Windows, Ctrl-C reaches it only between items in
a threaded step, so a step mid-way through a slow call can take a while to stop.)

A run can also hang without failing (a smoke run sat for an hour on 16 flex calls that
never answered). When neither the usage log nor the checkpoints change for
WSI_STALL_MINUTES (default 30), keep-going stops the child and its workers and resumes;
the checkpoints keep what was done. Like StageIncomplete, a stall never counts as a
repeating bug.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

from .run_manifest import MANIFEST

BACKOFF_S = (60, 300, 900, 1800)


def _manifest(data: Path) -> dict:
    try:
        return json.loads((data / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _option(args: List[str], name: str) -> Optional[str]:
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


STALLED = 75  # what _call returns when it stopped a child that had gone quiet
CTRL_C_EXIT = 0xC000013A  # how a process stopped by Ctrl-C exits on Windows


def _last_activity(data: Path) -> float:
    paths = [Path(os.getenv("WSI_USAGE_LOG") or data / "usage_log.jsonl"), data / "checkpoints"]
    paths += list((data / "checkpoints").glob("*.jsonl")) if (data / "checkpoints").is_dir() else []
    return max((p.stat().st_mtime for p in paths if p.exists()), default=0.0)


def _call(cmd: List[str], data: Path = Path("pipeline/data")) -> int:
    """Run cmd; stop it and its pool workers when the run goes quiet for WSI_STALL_MINUTES.
    On macOS and Linux it gets its own process group, so they can be stopped together. On
    Windows it stays in our console, so Ctrl-C reaches it and its workers directly."""
    stall_s = float(os.getenv("WSI_STALL_MINUTES", "30")) * 60
    started = time.time()
    proc = subprocess.Popen(cmd, start_new_session=os.name != "nt")
    try:
        while True:
            try:
                return proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            quiet = time.time() - max(_last_activity(data), started)
            if quiet > stall_s:
                print(f"[keep-going] no activity for {quiet / 60:.0f} min; stopping the run to resume it",
                      flush=True)
                _stop_group(proc, signal.SIGTERM, 60)
                return STALLED
    except KeyboardInterrupt:
        if os.name == "nt":
            # The console gave the child the Ctrl-C too; anything sent from here would kill it.
            try:
                proc.wait(timeout=600)  # let it finish what it is writing
            except subprocess.TimeoutExpired:
                _stop_tree(proc, 0)
        else:
            _stop_group(proc, signal.SIGINT, 600)  # let it finish what it is writing
        raise


def _stop_group(proc, sig, grace_s: float) -> None:
    if os.name == "nt":
        _stop_tree(proc, grace_s)
    else:
        try:
            os.killpg(proc.pid, sig)
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        except ProcessLookupError:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # pool workers left behind
        except ProcessLookupError:
            pass


def _stop_tree(proc, grace_s: float) -> None:
    """Stop proc and every process under it (pool workers, a browser). For Windows, which
    has no process groups to signal and doesn't give orphans a new parent, so the tree is
    listed before anything in it is stopped."""
    import psutil
    try:
        root = psutil.Process(proc.pid)
        tree = [root] + root.children(recursive=True)
        born = root.create_time()
    except psutil.NoSuchProcess:
        return
    for p in tree:  # the root first, so it can't start more
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
    for p in psutil.wait_procs(tree, timeout=grace_s)[1]:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    # Anything a dying process started after the list was made.
    stopped, found = {p.pid for p in tree}, True
    while found:
        found = False
        for p in psutil.process_iter(["ppid", "create_time"]):
            if p.info["ppid"] in stopped and p.pid not in stopped and (p.info["create_time"] or 0) >= born:
                stopped.add(p.pid)
                found = True
                try:
                    p.kill()
                except psutil.NoSuchProcess:
                    pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _keep_awake(on: bool) -> None:
    """Windows: keep the machine from sleeping while keep-going runs (what `caffeinate -i`
    does on macOS)."""
    if os.name == "nt":
        import ctypes
        es_continuous, es_system_required = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | (es_system_required if on else 0))


def supervise(args: List[str], max_restarts: int = 20, data: Path = Path("pipeline/data"),
              sleep=time.sleep, call=_call) -> int:
    """Run `papers *args` until it completes; returns the last exit code."""
    _keep_awake(True)
    try:
        return _supervise(args, max_restarts, data, sleep, call)
    finally:
        _keep_awake(False)


def _supervise(args: List[str], max_restarts: int, data: Path, sleep, call) -> int:
    run_id = _option(args, "--run-id")
    errors: List[str] = []
    code = 0
    for restart in range(max_restarts + 1):
        sessions_before = len(_manifest(data).get("sessions", []))
        cmd = [sys.executable, "-m", "pipeline.src.cli", "papers", *args]
        if run_id and not _option(args, "--run-id"):
            cmd += ["--run-id", run_id]
        print(f"[keep-going] {'starting' if restart == 0 else 'resuming'}: {' '.join(cmd[3:])}", flush=True)
        try:
            code = call(cmd)
        except KeyboardInterrupt:
            print("[keep-going] interrupted; not restarting", flush=True)
            return 130
        if code == 0:
            print(f"[keep-going] run {run_id or _manifest(data).get('run_id', '')} completed", flush=True)
            return 0
        manifest = _manifest(data)
        sessions = manifest.get("sessions", [])
        if len(sessions) <= sessions_before:
            print(f"[keep-going] the run did not start (exit {code}); see the message above. Not restarting.",
                  flush=True)
            return code
        run_id = run_id or manifest.get("run_id")
        last = sessions[-1]
        error = last.get("error") or (f"process died (exit {code})" if last.get("status") == "running"
                                      else f"exit {code}")
        if code == STALLED:
            error = "stalled (no activity), stopped by keep-going"
        if code != STALLED and (code in (130, -2, CTRL_C_EXIT) or "KeyboardInterrupt" in error):
            print("[keep-going] interrupted; not restarting", flush=True)
            return code
        errors.append("" if "StageIncomplete" in error or code == STALLED else error)
        if len(errors) >= 3 and len(set(errors[-3:])) == 1 and errors[-1]:
            print(f"[keep-going] the same error stopped the last 3 sessions; not restarting:\n  {error}", flush=True)
            return code
        if restart == max_restarts:
            break
        wait = BACKOFF_S[min(restart, len(BACKOFF_S) - 1)]
        print(f"[keep-going] run {run_id} stopped: {error}\n"
              f"[keep-going] resuming in {wait // 60} min (restart {restart + 1} of {max_restarts})", flush=True)
        sleep(wait)
    print(f"[keep-going] gave up after {max_restarts} restarts", flush=True)
    return code
