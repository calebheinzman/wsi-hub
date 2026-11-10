import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

_LOGGER_CONFIGURED = False

def get_logger(name: str) -> logging.Logger:
    """Get a module-level logger with a consistent format.

    Ensures handlers are added only once across the application.
    """
    global _LOGGER_CONFIGURED
    logger = logging.getLogger(name)
    if not _LOGGER_CONFIGURED:
        logger_root = logging.getLogger()
        # Default to WARNING to keep output quiet unless explicitly enabled
        logger_root.setLevel(logging.WARNING)
        handler = logging.StreamHandler()
        handler.setLevel(logging.WARNING)
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger_root.addHandler(handler)
        _LOGGER_CONFIGURED = True
    return logger

def configure_logging(verbose: bool = False) -> None:
    """Configure global logging verbosity and quiet noisy third-party loggers.

    - When verbose is False (default), only show warnings and above.
    - When verbose is True, show info-level logs across the app and libraries.
    """
    global _LOGGER_CONFIGURED
    # Ensure base handler exists
    if not _LOGGER_CONFIGURED:
        get_logger(__name__)

    level = logging.INFO if verbose else logging.WARNING
    logging.getLogger().setLevel(level)

    # Ensure existing handlers do not continue filtering at WARNING when verbose is enabled.
    for handler in logging.getLogger().handlers:
        handler.setLevel(level)

    # Quiet or enable common noisy libraries
    noisy_libs = [
        "httpx",
        "openai",
        "google",
        "google_genai",
        "google.genai",
        "generativelanguage",
    ]
    for name in noisy_libs:
        logging.getLogger(name).setLevel(level if verbose else logging.WARNING)


def add_file_logging(log_file: str, verbose: bool = False) -> None:
    """Add a file handler to the root logger (idempotent)."""
    global _LOGGER_CONFIGURED

    if not _LOGGER_CONFIGURED:
        get_logger(__name__)

    level = logging.INFO if verbose else logging.WARNING
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == str(log_path):
            h.setLevel(level)
            return

    fh = logging.FileHandler(str(log_path), encoding="utf-8")
    fh.setLevel(level)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh.setFormatter(formatter)
    root.addHandler(fh)

def utf8_stdio() -> None:
    """On Windows, print as UTF-8 and start child processes (pool workers, a resumed run, the
    scorers) in UTF-8 mode. Redirected output there is cp1252, which can't hold a ✓, an →
    or most paper titles, so one such print would raise. Nothing to do elsewhere."""
    if os.name != "nt":
        return
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="backslashreplace")

def _free_commit_mb() -> float:
    """Windows: memory that can still be committed (RAM + page file), in MB."""
    import ctypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [
            (f, ctypes.c_ulonglong) for f in ("total_phys", "avail_phys", "total_commit", "avail_commit",
                                              "total_virtual", "avail_virtual", "avail_extended")]

    status = MemoryStatus(dwLength=ctypes.sizeof(MemoryStatus))
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return status.avail_commit / 2**20

def pool_size(n: int) -> int:
    """n worker processes, on Windows capped at 61 (the most a pool can wait on there) and at
    what the free commit holds at WSI_WORKER_MB (150) a worker, keeping 2 GB spare. Windows
    refuses an allocation past its commit limit rather than paging, so an oversized pool
    crashes. Workers start with OPENBLAS_NUM_THREADS=1: numpy's OpenBLAS otherwise commits
    ~500 MB per process on a 16-thread CPU, which 24 workers can't afford."""
    if os.name != "nt":
        return n
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    per_worker = float(os.getenv("WSI_WORKER_MB", "150"))
    fits = max(1, int((_free_commit_mb() - 2048) // per_worker))
    if fits < n:
        logging.getLogger(__name__).warning(
            f"{n} worker processes would exhaust memory; using {fits} (close apps, or a bigger page file, for more)")
    return min(n, 61, fits)

def read_lines(path):
    """Read lines from a file, stripping whitespace and removing empty lines."""
    with open(path, 'r', encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

def read_json(path):
    """Read a JSON file."""
    with open(path, 'r', encoding="utf-8") as f:
        return json.load(f)

def atomic_json(path, data, indent=4):
    """Write data to a JSON file atomically (a temp file, then a rename). A stage file a
    crash cut off would otherwise be read on resume as the stage's finished output."""
    tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
    with open(tmp, 'w', encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=indent)
    replace_retry(tmp, path)


def atomic_text(path, text: str) -> None:
    """Write text to a file atomically, as UTF-8 with LF line ends on every OS."""
    tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
    with open(tmp, 'w', encoding="utf-8", newline="\n") as f:
        f.write(text)
    replace_retry(tmp, path)


def replace_retry(src, dst, tries: int = 10, wait: float = 0.1) -> None:
    """os.replace, riding out Windows refusing it for a moment while another process (a
    reader, the virus scanner) has the destination open. On the last failure the temp
    file is removed and the error raised."""
    for attempt in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == tries - 1:
                Path(src).unlink(missing_ok=True)
                raise
            time.sleep(wait)


def write_json(path, data):
    """Write data to a JSON file (atomically)."""
    atomic_json(path, data, indent=4)
