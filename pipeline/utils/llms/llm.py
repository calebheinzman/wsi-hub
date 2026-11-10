import os
import sys
import json
import time
import random
import logging
import ast
import hashlib
import uuid
import re
from openai import OpenAI
from pathlib import Path
from typing import List, Dict, Optional, Callable, Any, Tuple

import base64

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)


# ============================================================================
# JSON Validation Utilities
# ============================================================================

def strip_json_wrapper(content: str) -> str:
    """Recover a bare JSON document from a model response.

    `gemini-3.1-pro-preview` ignores both `response_mime_type=application/json` and an
    explicit "no code fences" instruction, and wraps its output in a markdown fence --
    where `gemini-2.5-pro` returns clean JSON. It also sometimes omits the closing
    brace. Every structured step (1.4a/b/c, 1.7, 1.8, dataset validation, ex_data)
    routes through here, so unwrapping centrally is what keeps the pro model usable.

    Handles, in order: markdown fences, leading prose before the first brace/bracket,
    trailing prose after the last one, and a single missing closing brace/bracket.
    """
    s = (content or "").strip()
    if not s:
        return s

    # ```json ... ``` or ``` ... ```
    fence = re.match(r"^```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?\s*```$", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    else:
        # Unterminated fence (truncated response)
        s = re.sub(r"^```[a-zA-Z0-9_-]*\s*\n?", "", s)
        s = re.sub(r"\n?\s*```$", "", s).strip()

    # Trim prose either side of the outermost JSON value.
    start = min((i for i in (s.find("{"), s.find("[")) if i != -1), default=-1)
    if start > 0:
        s = s[start:]
    end = max(s.rfind("}"), s.rfind("]"))
    if end != -1 and end < len(s) - 1:
        s = s[:end + 1]
    s = s.strip()

    # Repair a single unclosed object/array (observed on gemini-3.1-pro-preview,
    # which stops with finish_reason=stop yet omits the final brace).
    if s.startswith("{") and s.count("{") == s.count("}") + 1 and not s.endswith("}"):
        s = re.sub(r"[\s,]+$", "", s) + "}"
    elif s.startswith("[") and s.count("[") == s.count("]") + 1 and not s.endswith("]"):
        s = re.sub(r"[\s,]+$", "", s) + "]"

    return s


def strip_code_fence(content: str) -> str:
    """Remove a markdown code fence from a response that should be a bare string.

    For prompts whose contract is plain prose (e.g. `sum_page.md`), which models fence
    anyway -- `validate_run.py` found fenced summaries stored verbatim in the January
    corpus. Unlike `strip_json_wrapper` this does not touch the body.
    """
    s = (content or "").strip()
    if not s.startswith("```"):
        return s
    s = re.sub(r"^```[a-zA-Z0-9_-]*\s*\n?", "", s)
    return re.sub(r"\n?\s*```$", "", s).strip()


def is_valid_json_response(content: str) -> Tuple[bool, Optional[Any], Optional[str]]:
    """
    Check if content is valid JSON and not empty/trivial.

    Returns:
        Tuple of (is_valid, parsed_dict, error_message)
    """
    if not content or not content.strip():
        return False, None, "Empty response content"

    cleaned = strip_json_wrapper(content)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as first_error:
        # Models sometimes emit two complete documents back to back
        # ("{...}\n{...}", seen as "Extra data: line 49"). Trimming to the last
        # closing brace keeps both, so decode just the first value and drop the
        # rest -- a second object is a duplicate answer, not extra information.
        try:
            parsed, _ = json.JSONDecoder().raw_decode(cleaned)
            logger.debug("Took the first of multiple JSON documents in one response")
        except json.JSONDecodeError:
            return False, None, f"Invalid JSON: {first_error}"
    try:
        parsed = normalize_json_tree(parsed)
    except Exception as e:
        return False, None, f"Invalid JSON: {e}"

    # gemini-3.1-pro-preview intermittently wraps the object in a one-element array
    # (~1 in 3 on paper_classification), which parses fine but is a list, producing the
    # "'list' object has no attribute 'get'" failure. Every
    # prompt in config/prompts specifies a top-level object, so a single-element array
    # is always this wrapper rather than a legitimate response.
    if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
        logger.debug("Unwrapped single-element array wrapper from model response")
        parsed = parsed[0]
    
    if not isinstance(parsed, (dict, list)):
        return False, None, f"Expected dict or list, got {type(parsed).__name__}"
    
    return True, parsed, None


def _clean_string_scalar(value: str) -> str:
    s = value.strip()
    if not s:
        return ""

    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"', "`"):
        s = s[1:-1].strip()

    while s and s[0] in ('"', "'", "`"):
        if len(s) >= 2 and s[0] == s[-1]:
            break
        s = s[1:].lstrip()

    s = s.strip()
    if s.endswith(","):
        s = re.sub(r"[\s,]+$", "", s).strip()

    lowered = s.strip().lower()
    if lowered in {"nan", "null", "none", "n/a", "unknown"}:
        return ""
    if lowered == "0":
        return ""
    return s


def _try_parse_jsonish_string(value: str) -> Any:
    s = value.strip()
    if not s:
        return value

    candidates = [s]
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        candidates.append(s[1:-1].strip())

    def _candidate_variants(cand: str) -> list[str]:
        variants = [cand]
        if '\\"' in cand:
            variants.append(cand.replace('\\"', '"'))
        if "\\'" in cand:
            variants.append(cand.replace("\\'", "'"))
        return variants

    for cand in candidates:
        if not cand:
            continue
        if cand[0] in "[{" and cand[-1] in "]}":
            for v in _candidate_variants(cand):
                try:
                    return json.loads(v)
                except Exception:
                    pass

    for cand in candidates:
        if not cand:
            continue
        if cand[0] in "[{" and cand[-1] in "]}":
            for v in _candidate_variants(cand):
                try:
                    parsed = ast.literal_eval(v)
                    if isinstance(parsed, (dict, list)):
                        return parsed
                except Exception:
                    pass

    return value


def _clean_key(key: str) -> str:
    """Strip whitespace and wrapping quotes from a key, nothing more.

    Keys must not get the value sentinels in _clean_string_scalar: there "0" and
    "none" mean "unstated" and become "", but as keys they are real names. The
    structured gate's criterion keys are "0", "1", ...; blanking "0" made every
    sync structured-gate answer read as failing criterion 0, so it kept nothing.
    """
    s = key.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"', "`"):
        s = s[1:-1].strip()
    return s or key


def normalize_json_tree(obj: Any) -> Any:
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str):
                nk = _clean_key(k)
            else:
                nk = str(k)
            out[nk] = normalize_json_tree(v)
        return out

    if isinstance(obj, list):
        return [normalize_json_tree(x) for x in obj]

    if isinstance(obj, str):
        cleaned = _clean_string_scalar(obj)
        parsed = _try_parse_jsonish_string(cleaned)
        if parsed is not cleaned:
            return normalize_json_tree(parsed)
        return cleaned

    return obj


def is_all_empty_values(data: Any) -> bool:
    """
    Check if all values in a dict are empty/null.
    
    An empty value is: None, empty string, empty list, empty dict, 
    or a dict where all values are empty (recursive).
    """
    if data is None:
        return True
    if isinstance(data, (list, tuple)):
        if len(data) == 0:
            return True
        return all(is_all_empty_values(v) for v in data)
    if not isinstance(data, dict):
        if isinstance(data, str):
            return not data.strip()
        return False
    def _is_empty(val: Any) -> bool:
        if val is None:
            return True
        if isinstance(val, str) and not val.strip():
            return True
        if isinstance(val, (list, tuple)) and len(val) == 0:
            return True
        if isinstance(val, (dict, list, tuple)):
            return is_all_empty_values(val)
        return False
    
    return all(_is_empty(v) for v in data.values())


def validate_json_response(
    content: str,
    required_fields: Optional[List[str]] = None,
    reject_all_empty: bool = True
) -> Tuple[bool, Optional[Any], Optional[str]]:
    """
    Validate a JSON response from LLM.
    
    Args:
        content: Raw response content string
        required_fields: Optional list of field names that must be present
        reject_all_empty: If True, reject responses where all values are empty
        
    Returns:
        Tuple of (is_valid, parsed_dict, error_message)
    """
    # First check basic JSON validity
    is_valid, parsed, error = is_valid_json_response(content)
    if not is_valid:
        return False, None, error

    # Common LLM formatting failure mode: wrap the object in a single-element list.
    # Preserve legacy callers that expect a dict by unwrapping [{...}] -> {...}.
    if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
        parsed = parsed[0]
    
    # Check required fields (only applies to JSON objects)
    if required_fields:
        if not isinstance(parsed, dict):
            return False, parsed, "Required fields check expects a JSON object"
        missing = [f for f in required_fields if f not in parsed]
        if missing:
            return False, parsed, f"Missing required fields: {missing}"
    
    # Check if all values are empty
    if reject_all_empty and is_all_empty_values(parsed):
        return False, parsed, "All values are empty/null"
    
    return True, parsed, None

# Import batch manager (lazy to avoid circular imports)
_BATCH_PROCESSOR = None

# Lazily-initialized OpenAI client (safer for multiprocessing spawn on macOS)
_CLIENT = None
_GEMINI_CLIENT = None

# WSI_USAGE_LOG=<path> appends one JSON line per API call (and per full-text fetch, from
# filters.load_full_text): tokens, latency and the pipeline function that made it. An
# env var rather than a hook because extraction runs in a multiprocessing pool, whose
# workers don't inherit module state. Unset, nothing is written.
def _caller() -> str:
    f = sys._getframe(2)
    while f is not None:
        name = f.f_code.co_filename.replace("\\", "/")  # Windows paths
        if "/pipeline/src/" in name:
            return f"{Path(name).stem}.{f.f_code.co_name}"
        f = f.f_back
    return ""


def log_event(kind: str, **fields) -> None:
    path = os.getenv("WSI_USAGE_LOG")
    if not path:
        return
    rec = {"kind": kind, "ts": round(time.time(), 3), "pid": os.getpid(), "caller": _caller(), **fields}
    _append(path, json.dumps(rec, default=str) + "\n")


# WSI_LLM_LEDGER=<path> (`cli papers` sets pipeline/data/llm_calls.jsonl) appends one line per
# synchronous call: which item it was for, the model asked for and the one that answered,
# the request id, the settings sent, hashes of the prompts and attachments (the prompts
# themselves go to the archive, pipeline.src.provenance), and the answer as it came back,
# before any JSON repair. The usage log keeps only tokens and time; this is what lets a
# published value be traced to the call that produced it. Batch answers are kept by the
# batch manager (batch_outputs/).
_ITEM_LINE = re.compile(r"^(?:Dataset|Model) Name: *(.+)$", re.M)


def _ledger(**fields) -> bool:
    """Append a ledger line; True when written (or no ledger is kept)."""
    path = os.getenv("WSI_LLM_LEDGER")
    if not path:
        return True
    rec = {"ts": round(time.time(), 3), "pid": os.getpid(), "caller": _caller(), **fields}
    if _append(path, json.dumps(rec, default=str) + "\n"):
        return True
    logger.warning("LLM ledger: could not write to %s", path)
    return False


class ResponseNotSaved(BaseException):
    """An answered call that neither the ledger nor the archive could record. Raised rather
    than returned: an answer with no record would enter the catalog untraceable. A
    BaseException, so that no stage's `except Exception` fallback (a failed canonicalization
    call becomes singleton groups, saved as done) can swallow it: it stops the run, which
    `cli papers` records as failed. Finished items are checkpointed; `keep-going` resumes
    and the item runs again, and gives up if the same error ends three sessions (a disk
    that keeps refusing)."""


def _record_answer(**fields) -> None:
    """The ledger line of an answered call, and the same record archived on its own
    (provenance.persist_response): two copies, so verify_run can tell when one is lost."""
    from pipeline.src.provenance import persist_response
    saved = persist_response({"ts": round(time.time(), 3), **fields})
    if not _ledger(**fields) and not saved:
        raise ResponseNotSaved(f"could not record call {fields.get('call_id')} for {fields.get('item')!r}")


def _call_identity(item: str, system_prompt: str, user_prompt: str, attachments, kwargs: dict) -> dict:
    """The ledger's description of one request (the same for each of its attempts)."""
    if not os.getenv("WSI_LLM_LEDGER"):
        return {}
    from pipeline.src.provenance import archive_bytes, archive_text
    if not item:  # the dataset prompts open with the dataset's name
        m = _ITEM_LINE.search((user_prompt or "")[:500])
        item = m.group(1).strip() if m else ""
    rf = kwargs.get("response_format")
    params = {k: kwargs[k] for k in ("temperature", "service_tier", "reasoning_effort") if k in kwargs}
    if rf:
        params["response_format"] = rf.get("type") if isinstance(rf, dict) else str(rf)
    return {"item": item, "model": kwargs.get("model"), "params": params,
            "system_sha256": archive_text("llm_prompt", system_prompt or ""),
            "user_sha256": archive_text("llm_input", user_prompt or ""),
            "attachments": [{"name": n, "sha256": archive_bytes("llm_attachment", d)} for n, d in attachments or []]}


def _append(path: str, line: str) -> bool:
    """Append one line; a few tries (a scanner can hold the file a moment). False if lost."""
    for wait in (0, 0.2, 1):
        time.sleep(wait)
        try:
            if os.name == "nt":
                _append_windows(path, line)
            else:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
            return True
        except OSError:
            pass
    return False


def _append_windows(path: str, line: str) -> None:
    """Append `line` in one write the kernel puts at the end of the file. Windows' C runtime
    appends by seeking to the end and then writing, so lines from the pool's processes can
    overwrite each other; a handle opened for FILE_APPEND_DATA alone can't."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    # FILE_APPEND_DATA; share read/write/delete; OPEN_ALWAYS; FILE_ATTRIBUTE_NORMAL
    h = k32.CreateFileW(str(path), 0x4, 0x7, None, 4, 0x80, None)
    if h is None or h == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        data = line.encode("utf-8")
        written = wintypes.DWORD()
        if not k32.WriteFile(h, data, len(data), ctypes.byref(written), None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        k32.CloseHandle(h)


def _usage_fields(usage) -> dict:
    details = getattr(usage, "completion_tokens_details", None)
    return {"input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "cached_tokens": getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0,
            "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "reasoning_tokens": getattr(details, "reasoning_tokens", 0) or 0}


def _get_openai_client():
    """Return a per-process OpenAI client with sane defaults.

    Avoids creating the client at import-time so child processes (spawn) don't
    """
    global _CLIENT
    if _CLIENT is None:
        # Timeout in seconds; configurable via env for long JSON responses. Flex calls run
        # ~3x slower (p99 131 s, max 260 s in the smoke runs), so 300 s; a flex call that
        # times out goes again at the default tier (run_gpt). The SDK's own retries are off
        # on flex: with them, a hung call waited 3 x the timeout before run_gpt saw it (in
        # one smoke run 16 calls hung at once and the stage sat for an hour).
        api_key = os.getenv("OPENAI_API_KEY", "")
        flex = os.getenv("WSI_OPENAI_SERVICE_TIER", "flex") == "flex"
        timeout_s = float(os.getenv("WSI_OPENAI_TIMEOUT", "300" if flex else "120"))
        _CLIENT = OpenAI(api_key=api_key, timeout=timeout_s, max_retries=0 if flex else 2)
    return _CLIENT

def _get_gemini_client():
    """Return a per-process Gemini client using OpenAI-compatible API.
    
    Uses the OpenAI SDK with Gemini's base URL for compatibility.
    """
    global _GEMINI_CLIENT
    if _GEMINI_CLIENT is None:
        api_key = os.getenv("GEMINI_API_KEY", "")
        timeout_s = float(os.getenv("WSI_OPENAI_TIMEOUT", "120"))
        _GEMINI_CLIENT = OpenAI(
            api_key=api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            timeout=timeout_s
        )
    return _GEMINI_CLIENT

def run_gpt(
    system_prompt: str,
    user_prompt: str,
    model: str = "gpt-6-luna",
    response_format=None,
    use_gemini: bool = False,
    validate_json: bool = False,
    required_fields: Optional[List[str]] = None,
    reject_all_empty: bool = True,
    max_retries: int = 3,
    attachments: Optional[List[Tuple[str, bytes]]] = None,
    item: str = "",
):
    """Call OpenAI or Gemini with a system + user message and return a ChatCompletion-like object.

    Returns an object compatible with callers expecting `response.choices[0].message.content`.
    Uses Chat Completions for stability; if `response_format` is provided, it is passed through
    when supported by the SDK (e.g., {"type": "json_object"} or json_schema config).
    
    Args:
        system_prompt: The system prompt.
        user_prompt: The user prompt.
        model: The model name. If use_gemini is True, should be a Gemini model name.
        response_format: Optional response format specification.
        use_gemini: If True, use Gemini API instead of OpenAI.
        validate_json: If True and response_format requests JSON, validate the response.
        required_fields: Optional list of fields that must be present in JSON response.
        reject_all_empty: If True, reject JSON responses where all values are empty.
        max_retries: Maximum retry attempts for JSON validation failures (default 3).
        attachments: (filename, bytes) PDFs sent with the user message as file parts
            (OpenAI only), e.g. a paper whose tables are images.
        item: what the call is about (a paper's link, a dataset's name), for the ledger.
    """
    if use_gemini:
        client = _get_gemini_client()
        # Map GPT model names to Gemini equivalents if needed
        if model.startswith("gpt"):
            # Default Gemini fallback when caller passed a GPT name without specifying.
            model = "gemini-3-flash-preview"
        if model.startswith("models/"):
            model = model.split("/", 1)[1]
    else:
        client = _get_openai_client()

    # OpenAI requires that at least one message explicitly mentions "json" when using
    # response_format={"type": "json_object"}. Add a minimal hint if the prompts don't.
    try:
        rf_is_json_object = (
            response_format is not None
            and isinstance(response_format, dict)
            and response_format.get('type') == 'json_object'
        )
    except Exception:
        rf_is_json_object = False

    if rf_is_json_object:
        sys_text = system_prompt or ""
        usr_text = user_prompt or ""
        # OpenAI's json_object response_format is picky: ensure the literal substring
        # 'json' appears in at least one message.
        if ('json' not in sys_text) and ('json' not in usr_text):
            system_prompt = (sys_text + "\n\nReturn valid json.").strip()

    # A lone surrogate in the text makes the client's UTF-8 encode raise; drop them.
    system_prompt = (system_prompt or "").encode("utf-8", "ignore").decode("utf-8")
    user_prompt = (user_prompt or "").encode("utf-8", "ignore").decode("utf-8")

    # Build messages payload
    user_content = user_prompt or ""
    if attachments and not use_gemini:
        user_content = [{"type": "file", "file": {"filename": name,
                                                   "file_data": "data:application/pdf;base64,"
                                                   + base64.b64encode(data).decode()}}
                        for name, data in attachments] + [{"type": "text", "text": user_prompt or ""}]
    messages = [
        {"role": "system", "content": system_prompt or ""},
        {"role": "user", "content": user_content},
    ]

    # Prepare kwargs; some models don't accept overriding temperature (default=1 only)
    kwargs = {
        "model": model,
        "messages": messages,
    }
    # Only set temperature for models that support it
    lower_model = (model or "").lower()
    models_disallow_temp = (
        lower_model.startswith(("gpt-5", "gpt-6"))
        or "gemini-2.5-pro" in lower_model
        or "gemini-3" in lower_model
    )
    if not models_disallow_temp:
        kwargs["temperature"] = 0

    # Pass response_format to both OpenAI and Gemini (Gemini's OpenAI-compatible API supports it)
    if response_format is not None:
        kwargs["response_format"] = response_format
    # WSI_OPENAI_SERVICE_TIER (default flex) bills OpenAI calls at batch prices on the
    # normal synchronous API (slower; raise WSI_OPENAI_TIMEOUT with it). Set it empty
    # for the default tier.
    service_tier = os.getenv("WSI_OPENAI_SERVICE_TIER", "flex")
    if not use_gemini and service_tier:
        kwargs["service_tier"] = service_tier
    # WSI_OPENAI_REASONING_EFFORT (low | medium | high) overrides the model's default
    # reasoning effort; unset leaves it to the model, as the pipeline runs.
    if not use_gemini and os.getenv("WSI_OPENAI_REASONING_EFFORT"):
        kwargs["reasoning_effort"] = os.environ["WSI_OPENAI_REASONING_EFFORT"]
    
    # Determine if we should validate JSON
    should_validate = validate_json or (
        response_format is not None and 
        isinstance(response_format, dict) and 
        response_format.get('type') == 'json_object'
    )
    
    # Retry loop for JSON validation
    last_error = None
    resp = None
    # max_retries counts ATTEMPTS here, so 0 must still make one call. Without the
    # floor the loop body never runs and the `return resp` below raises
    # UnboundLocalError, masking whatever the API actually said.
    attempts = max(1, max_retries)
    identity = _call_identity(item, system_prompt, user_prompt,
                              attachments if not use_gemini else None, kwargs)
    for attempt in range(attempts):
        try:
            # Perform request
            t0 = time.time()
            try:
                resp = client.chat.completions.create(**kwargs)
            except Exception as e:
                # Flex answers 429 "does not have sufficient resources" when it is busy, and
                # sometimes never answers: either way the call goes again at the default
                # tier (list price) instead of failing or hanging.
                busy = "sufficient resources" in str(e)
                hung = "Timeout" in type(e).__name__
                if kwargs.get("service_tier") != "flex" or not (busy or hung):
                    raise
                log_event("flex_fallback", model=model, reason="timeout" if hung else "busy")
                if identity:  # the flex attempt was a request of its own
                    _ledger(**identity, attempt=attempt + 1, seconds=round(time.time() - t0, 2),
                            error=f"{type(e).__name__}: {str(e)[:300]}", fallback="default_tier")
                    identity = {**identity, "params": {k: v for k, v in identity["params"].items()
                                                       if k != "service_tier"}}
                kwargs.pop("service_tier")
                resp = client.chat.completions.create(**kwargs)
            seconds = round(time.time() - t0, 2)
            # One id per answered call, in both the usage log and the ledger, so that
            # provenance.verify_run can match them call by call, not by count.
            call_id = uuid.uuid4().hex
            usage = _usage_fields(resp.usage) if getattr(resp, "usage", None) is not None else {"usage": None}
            log_event("llm", model=model, tier=kwargs.get("service_tier", ""), seconds=seconds, call_id=call_id,
                      attempt=attempt + 1, pdf=bool(attachments and not use_gemini), **usage)
            raw = resp.choices[0].message.content if resp.choices else None
            choice = resp.choices[0] if resp.choices else None
            answered = {"call_id": call_id, "completion_id": getattr(resp, "id", None),
                        "request_id": getattr(resp, "_request_id", None),  # the provider's x-request-id
                        "model_returned": getattr(resp, "model", None),
                        "finish_reason": getattr(choice, "finish_reason", None),
                        "refusal": getattr(getattr(choice, "message", None), "refusal", None),
                        "system_fingerprint": getattr(resp, "system_fingerprint", None),
                        "tier_returned": getattr(resp, "service_tier", None), "seconds": seconds,
                        "attempt": attempt + 1, "raw": raw, **usage}

            # If no JSON validation needed, return as-is
            if not should_validate:
                if identity:
                    _record_answer(**identity, **answered, valid=None)
                return resp

            # Validate JSON response
            content = (resp.choices[0].message.content or "").strip()
            is_valid, parsed, error = validate_json_response(
                content,
                required_fields=required_fields,
                reject_all_empty=reject_all_empty
            )
            if identity:
                _record_answer(**identity, **answered, valid=is_valid, error=error)

            if is_valid:
                # Hand back exactly what validation accepted, re-serialized. Callers
                # read resp.choices[0].message.content directly and several json.loads
                # it themselves, so returning the raw text would mean validation passed
                # on the repaired value while the caller still saw the markdown fence,
                # the missing brace, or the one-element array wrapper. That would hide
                # the failure rather than fix it.
                try:
                    canonical = json.dumps(parsed)
                    if canonical != content:
                        logger.debug("Normalized model JSON for %s", model)
                        resp.choices[0].message.content = canonical
                except Exception:
                    pass  # some SDK response objects are frozen
                return resp
            
            # Log validation failure
            last_error = error
            logger.warning(
                f"JSON validation failed (attempt {attempt + 1}/{attempts}): {error}. "
                f"Content preview: {content[:200]}..."
            )
            
            # Don't retry on last attempt
            if attempt < attempts - 1:
                sleep_s = min(2.0 * (2 ** attempt), 10.0) + random.random() * 0.25
                time.sleep(sleep_s)
                
        except ResponseNotSaved:
            raise  # another attempt would pay for an answer that can't be recorded either
        except Exception as e:
            last_error = str(e)
            if identity:
                _ledger(**identity, attempt=attempt + 1, error=f"{type(e).__name__}: {str(e)[:300]}")
            # The machine went offline (not the API failing): wait for the network rather
            # than spend the attempts, and every paper in the pool, in seconds.
            if "Connection" in type(e).__name__ or "Timeout" in type(e).__name__:
                from pipeline.utils.scraping.http import online, wait_for_network
                if not online() and wait_for_network("OpenAI" if not use_gemini else "Gemini"):
                    continue
            if attempt < attempts - 1:
                sleep_s = min(2.0 * (2 ** attempt), 10.0) + random.random() * 0.25
                time.sleep(sleep_s)
            else:
                log_event("llm_error", model=model, error=f"{type(e).__name__}: {str(e)[:200]}")
                raise
    
    # If we get here, all retries failed - return last response anyway
    # (caller can handle the invalid response)
    logger.warning(f"JSON validation failed after {attempts} attempts: {last_error}")
    if resp is None:
        raise RuntimeError(f"run_gpt made no successful call: {last_error}")
    return resp


def _get_batch_processor(use_gemini: bool = False):
    """Get or create batch processor instance."""
    from .batch_manager import BatchProcessor
    provider = "gemini" if use_gemini else "gpt"
    return BatchProcessor(provider=provider)


def run_batch_requests(requests: List[Dict],
                       step_name: str,
                       use_gemini: bool = False,
                       model: Optional[str] = None,
                       poll_interval: int = 120) -> Dict[str, Dict]:
    """Run a batch of LLM requests and return results.
    
    This function:
    1. Checks for cached results
    2. Resumes pending jobs if interrupted
    3. Creates new batch job if needed
    4. Polls until completion
    5. Caches and returns results
    
    Args:
        requests: List of request dicts with 'custom_id', 'system_prompt', 'user_prompt', etc.
        step_name: Name of the pipeline step (e.g., 'fulltext_filter_1_2')
        use_gemini: If True, use Gemini API instead of OpenAI
        model: Model to use (optional, uses defaults if not specified)
        poll_interval: Seconds between status polls (default 120)
        
    Returns:
        Dict mapping custom_id to result dict with 'response' or 'error' keys
    """
    processor = _get_batch_processor(use_gemini)
    
    # Use the unified get_or_create_batch method
    results = processor.get_or_create_batch(
        requests=requests,
        step_name=step_name,
        model=model,
        metadata={'description': step_name}
    )
    
    return results


def _extract_batch_content(result: Dict, use_gemini: bool) -> Optional[str]:
    """Extract content string from a batch result entry.
    
    Args:
        result: Batch result dict with 'response' key
        use_gemini: If True, parse Gemini format, else GPT format
        
    Returns:
        Content string or None if extraction failed
    """
    if not result or 'response' not in result:
        return None
    
    try:
        if use_gemini:
            response_obj = result.get('response', {})
            if 'candidates' in response_obj:
                return response_obj['candidates'][0]['content']['parts'][0]['text']
            return None
        else:
            response_obj = result.get('response', {})
            body = response_obj.get('body', {})
            return body.get('choices', [{}])[0].get('message', {}).get('content', '')
    except (KeyError, IndexError, TypeError):
        return None


def batch_extract(items: List[tuple],
                  prompt: str,
                  step_name: str,
                  use_gemini: bool = False,
                  model: Optional[str] = None,
                  reject_all_empty: bool = True,
                  required_fields: Optional[List[str]] = None,
                  max_retries: int = 2) -> List[Optional[Dict]]:
    """Batch version of extraction for multiple items with validation and retry support.
    
    Args:
        items: List of (item_id, text) tuples to extract from
        prompt: The system prompt to use
        step_name: Name of the pipeline step
        use_gemini: If True, use Gemini API instead of OpenAI
        model: Model to use (optional)
        reject_all_empty: If True, reject responses where all values are empty (default True)
        required_fields: Optional list of fields that must be present
        max_retries: Maximum retry attempts for failed items (default 2)
        
    Returns:
        List of extracted JSON dicts in same order as items (None on error)
    """
    # Determine model
    if model is None:
        if use_gemini:
            # gemini-2.5-pro, not 3.1-pro-preview. Measured on the same extraction
            # input, 3 calls each: 2.5-pro costs $0.0249/call vs $0.0536 (half the
            # thinking tokens, 1,506 vs 3,053) and returned clean parseable JSON 3/3
            # vs 2/3 -- 3.1-pro-preview ignores JSON mode and emits fences and
            # one-element array wrappers. It is also the model the January 2026
            # corpus was extracted with, so re-running keeps the data consistent.
            # Over a full run the difference is ~$187 vs ~$355 (batch pricing).
            # Override with WSI_EXTRACTION_MODEL to try a newer model.
            model_name = os.getenv('WSI_EXTRACTION_MODEL', 'gemini-2.5-pro')
        else:
            model_name = os.getenv('WSI_EXTRACTION_MODEL', 'gpt-6-luna')
    else:
        model_name = model
    
    # Initialize output with None to track which items need processing
    output = [None] * len(items)
    item_id_to_index = {str(item_id): idx for idx, (item_id, _) in enumerate(items)}
    items_to_process = list(items)
    
    for attempt in range(max_retries + 1):
        if not items_to_process:
            break
        
        if attempt > 0:
            logger.info(f"Retry attempt {attempt + 1}/{max_retries + 1} for {len(items_to_process)} items in '{step_name}'")
        
        # Build batch requests for remaining items
        requests = []
        for item_id, text in items_to_process:
            requests.append({
                'custom_id': str(item_id),
                'system_prompt': prompt,
                'user_prompt': text,
                'model': model_name,
                'response_format': {'type': 'json_object'}
            })
        
        # Run batch with retry-specific step name
        retry_step_name = f"{step_name}_retry{attempt}" if attempt > 0 else step_name
        results = run_batch_requests(
            requests=requests,
            step_name=retry_step_name,
            use_gemini=use_gemini,
            model=model_name
        )
        
        # Parse and validate results
        next_items_to_process = []
        for item_id, text in items_to_process:
            idx = item_id_to_index[str(item_id)]
            result = results.get(str(item_id))
            content = _extract_batch_content(result, use_gemini)
            
            if content is None:
                # Failed to extract content - needs retry
                logger.warning(f"No content in batch result for item {item_id} (attempt {attempt + 1})")
                next_items_to_process.append((item_id, text))
                continue
            
            # Validate JSON response
            is_valid, parsed, error = validate_json_response(
                content,
                required_fields=required_fields,
                reject_all_empty=reject_all_empty
            )
            
            if not is_valid:
                logger.warning(f"JSON validation failed for item {item_id} (attempt {attempt + 1}): {error}")
                next_items_to_process.append((item_id, text))
                continue
            
            # Valid response
            output[idx] = parsed
        
        items_to_process = next_items_to_process
    
    # Log final failures
    if items_to_process:
        failed_ids = [str(item_id) for item_id, _ in items_to_process]
        logger.warning(f"Step '{step_name}': {len(items_to_process)} items failed after {max_retries + 1} attempts: {failed_ids[:10]}...")
    
    return output


def batch_extract_with_validation(items: List[tuple],
                                   prompt: str,
                                   step_name: str,
                                   validator: Optional[Callable[[Dict], Tuple[bool, Optional[str]]]] = None,
                                   use_gemini: bool = False,
                                   model: Optional[str] = None,
                                   max_retries: int = 2,
                                   reject_all_empty: bool = True,
                                   required_fields: Optional[List[str]] = None) -> List[Optional[Dict]]:
    """Batch extraction with schema validation and automatic retries for failed items.
    
    This function:
    1. Runs batch extraction on all items (with built-in JSON validation)
    2. Validates results using the provided custom validator function
    3. Retries only the failed items (up to max_retries times)
    4. Merges successful results from all attempts
    
    Note: Basic JSON validation (invalid JSON, all-empty responses) is handled by batch_extract.
    The custom validator is for additional schema-specific validation.
    
    Args:
        items: List of (item_id, text) tuples to extract from
        prompt: The system prompt to use
        step_name: Name of the pipeline step
        validator: Optional function that takes a dict and returns (is_valid, error_message)
        use_gemini: If True, use Gemini API instead of OpenAI
        model: Model to use (optional)
        max_retries: Maximum number of retry attempts (default 2, for 3 total attempts)
        reject_all_empty: If True, reject responses where all values are empty
        required_fields: Optional list of fields that must be present
        
    Returns:
        List of extracted JSON dicts in same order as items (None on error or validation failure)
    """
    # Initialize output with None for all items
    output = [None] * len(items)
    
    # Track which items still need processing
    items_to_process = list(items)
    item_id_to_index = {str(item_id): idx for idx, (item_id, _) in enumerate(items)}
    
    for attempt in range(max_retries + 1):
        if not items_to_process:
            break
            
        attempt_num = attempt + 1
        if attempt > 0:
            logger.info(f"Retry attempt {attempt_num}/{max_retries + 1} for {len(items_to_process)} failed items in step '{step_name}'")
        
        # Run batch extraction on remaining items
        # Note: batch_extract now handles its own retries internally for JSON validation
        # We set max_retries=0 here to avoid double-retrying, and handle retries at this level
        retry_step_name = f"{step_name}_v{attempt}" if attempt > 0 else step_name
        results = batch_extract(
            items=items_to_process,
            prompt=prompt,
            step_name=retry_step_name,
            use_gemini=use_gemini,
            model=model,
            reject_all_empty=reject_all_empty,
            required_fields=required_fields,
            max_retries=0  # No internal retries - we handle retries here
        )
        
        # Process results and identify failures
        next_items_to_process = []
        for (item_id, text), result in zip(items_to_process, results):
            idx = item_id_to_index[str(item_id)]
            
            if result is None:
                # Extraction failed (JSON parse error, empty response, etc.)
                logger.warning(f"Extraction failed on attempt {attempt_num} for item {item_id}: result is None")
                next_items_to_process.append((item_id, text))
                continue
            
            # Apply custom validator if provided
            if validator is not None:
                is_valid, error_msg = validator(result)
                if not is_valid:
                    logger.warning(f"Custom validation failed on attempt {attempt_num} for item {item_id}: {error_msg}")
                    next_items_to_process.append((item_id, text))
                    continue
            
            # Success - store result
            output[idx] = result
        
        items_to_process = next_items_to_process
    
    # Log final failures
    if items_to_process:
        failed_ids = [str(item_id) for item_id, _ in items_to_process]
        logger.warning(f"Step '{step_name}': {len(items_to_process)} items failed after {max_retries + 1} attempts: {failed_ids[:10]}...")
    
    return output
