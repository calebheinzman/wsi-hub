"""Per-item checkpoints, so a stage that stops part-way resumes where it stopped.

Each stage writes its output file when it finishes, and a resumed run skips the stages
whose file is there. Inside a stage there was nothing: the gate on 30-40k papers is a day
of fetching and calls, and a crash at hour 20 paid for all of it again. Now the items a
stage finishes are appended to `<WSI_CHECKPOINT_DIR>/<step>.jsonl` as they come back, and a
restarted stage only runs the rest.

Items that fail in a way that may pass -- the API or the network was down, a call raised
-- are not recorded. The stage retries them after each of WSI_RETRY_WAITS seconds
(default 60,300,900). If more than a few still fail (WSI_MAX_STAGE_FAILURES, default 5,
or 0.5% of the step's items), the step raises StageIncomplete instead of writing an output
that would silently lack them -- the next stage, and every resume, would take that output
as finished. `cli keep-going` then resumes the run later, and only the missing items run.
A handful of failures is tolerated: they get the stage's own fallback, as before, and are
recorded in failed_calls.json.

Checkpoints (and the retry waits) are on only when WSI_CHECKPOINT_DIR is set, which `cli
papers` does (pipeline/data/checkpoints). The evaluation scripts call the same stage
functions and must not reuse a run's answers.

The first line of a checkpoint file is its fingerprint (the prompt and model of the
step). A file whose fingerprint differs -- the prompt changed between sessions -- is moved
aside, not reused.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, List, Tuple

from tqdm import tqdm

from pipeline.src.utils import pool_size


def enabled() -> bool:
    return bool(os.getenv("WSI_CHECKPOINT_DIR"))


def fingerprint(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


class Checkpoint:
    """The finished items of one step: key -> JSON value, kept in memory and on disk."""

    def __init__(self, step: str, fp: str = ""):
        self.step, self.done = step, {}
        self._lock = threading.Lock()
        self._f = None
        root = os.getenv("WSI_CHECKPOINT_DIR")
        if not root:
            return
        self.path = Path(root) / f"{step}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                header = _loads(f.readline()) or {}
                if header.get("fingerprint") != fp:
                    f.close()
                    stamp, n = int(time.time()), 1
                    stale = self.path.with_name(f"{step}.stale-{stamp}.jsonl")
                    while stale.exists():  # a second one this second (Windows won't rename onto it)
                        stale, n = self.path.with_name(f"{step}.stale-{stamp}-{n}.jsonl"), n + 1
                    self.path.rename(stale)
                    print(f"[checkpoint] {step}: prompt or model changed since {stale.name} was written; "
                          f"starting the step over", flush=True)
                else:
                    for line in f:
                        rec = _loads(line)  # None for a line a crash cut off
                        if rec is not None:
                            self.done[rec["k"]] = rec["v"]
        if not self.path.exists():
            self.path.write_text(json.dumps({"step": step, "fingerprint": fp,
                                             "created": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n", encoding="utf-8")
        elif self.path.read_bytes()[-1:] != b"\n":
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("\n")  # end the line a crash cut off before appending after it
        if self.done:
            print(f"[checkpoint] {step}: {len(self.done)} items already done; resuming", flush=True)
        self._f = open(self.path, "a", encoding="utf-8")

    def __contains__(self, key: str) -> bool:
        return key in self.done

    def get(self, key: str, default: Any = None) -> Any:
        return self.done.get(key, default)

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self.done[key] = value
            if self._f is not None:
                self._f.write(json.dumps({"k": key, "v": value}, default=str) + "\n")
                self._f.flush()

    def close(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None


def _loads(line: str):
    try:
        return json.loads(line)
    except ValueError:
        return None


class StageIncomplete(RuntimeError):
    """A step ended with more failed items than it may leave out (see the module doc)."""


def is_permanent_error(exc: BaseException) -> bool:
    """An API error that a retry won't change: a malformed or too-long request (400, 404,
    422). Everything else (5xx, 429, timeouts, connection errors) may pass."""
    return type(exc).__name__ in ("BadRequestError", "NotFoundError", "UnprocessableEntityError")


def _allowed_failures(n_items: int) -> int:
    floor = int(os.getenv("WSI_MAX_STAGE_FAILURES", "5"))
    return max(floor, int(n_items * 0.005))


class ItemError:
    """What a runner yields for an item whose function raised. Picklable on purpose."""

    def __init__(self, error: str):
        self.error = error

    def __repr__(self) -> str:
        return f"ItemError({self.error!r})"


def _call(args):
    i, fn, item = args
    try:
        return i, fn(item)
    except Exception as exc:  # noqa: BLE001 - one item must not stop the stage
        return i, ItemError(f"{type(exc).__name__}: {exc}"[:300])


Runner = Callable[[Callable, list], Iterator[Tuple[Any, Any]]]


def process_runner(procs: int, desc: str = "") -> Runner:
    """Items through a process pool, yielded as they finish (fn must be module-level).

    concurrent.futures rather than multiprocessing.Pool: when a worker dies (killed for
    memory), Pool loses its task and waits forever; ProcessPoolExecutor raises
    BrokenProcessPool, the run stops, and keep-going resumes it from the checkpoint."""
    def run(fn, items):
        work = [(i, fn, it) for i, it in enumerate(items)]
        bar = tqdm(total=len(items), desc=desc)
        try:
            if procs <= 1 or len(items) <= 1:
                for i, res in map(_call, work):
                    bar.update()
                    yield items[i], res
            else:
                with ProcessPoolExecutor(max_workers=pool_size(procs)) as ex:
                    for fut in as_completed([ex.submit(_call, w) for w in work]):
                        i, res = fut.result()
                        bar.update()
                        yield items[i], res
        finally:
            bar.close()
    return run


def thread_runner(threads: int, desc: str = "") -> Runner:
    """Items through a thread pool, yielded as they finish."""
    def run(fn, items):
        bar = tqdm(total=len(items), desc=desc)
        try:
            with ThreadPoolExecutor(max_workers=max(1, threads)) as ex:
                futures = {ex.submit(_call, (i, fn, it)): i for i, it in enumerate(items)}
                for fut in as_completed(futures):
                    i, res = fut.result()
                    bar.update()
                    yield items[i], res
        finally:
            bar.close()
    return run


def retry_waits() -> List[float]:
    if not enabled():
        return []
    raw = os.getenv("WSI_RETRY_WAITS", "60,300,900")
    return [float(w) for w in raw.split(",") if w.strip()]


def run_items(step: str, work: list, key: Callable[[Any], str], fn: Callable, runner: Runner,
              failed: Callable[[Any], bool] = lambda res: False, fp: str = "") -> Tuple[dict, list]:
    """Run fn over work through runner, skipping items the step's checkpoint already has.

    An answer for which `failed(answer)` is true, or an ItemError, is a failure that may
    pass: it is not recorded, and the item is retried after each of retry_waits(). Returns
    ({key: answer} for every item that finished, [(item, last answer) for those that still
    failed])."""
    ck = Checkpoint(step, fp)
    pending = [w for w in _unique(work, key) if key(w) not in ck]
    failures: list = []
    try:
        for wait in [0.0] + retry_waits():
            if not pending:
                break
            if wait:
                print(f"[retry] {step}: {len(pending)} items failed; trying them again in {wait:.0f}s",
                      flush=True)
                time.sleep(wait)
            failures = []
            for item, res in runner(fn, pending):
                if isinstance(res, ItemError) or failed(res):
                    failures.append((item, res))
                else:
                    ck.put(key(item), res)
            pending = [item for item, _ in failures]
    finally:
        ck.close()
    if failures and enabled() and len(failures) > _allowed_failures(len(work)):
        sample = "; ".join(str(getattr(r, "error", None) or (r.get("error") if isinstance(r, dict) else r))[:120]
                           for _, r in failures[:3])
        raise StageIncomplete(f"{step}: {len(failures)} of {len(work)} items still failing after retries "
                              f"(e.g. {sample}); stopping so a resume runs them")
    if failures:
        print(f"[retry] {step}: {len(failures)} items still failing; continuing without them", flush=True)
    return dict(ck.done), failures


def _unique(work: Iterable, key) -> list:
    seen, out = set(), []
    for w in work:
        k = key(w)
        if k not in seen:
            seen.add(k)
            out.append(w)
    return out
