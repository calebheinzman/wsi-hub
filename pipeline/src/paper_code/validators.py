import json
import re
import ast
import unicodedata
from pathlib import Path

from .standardize import normalize_staining_type

SCHEMAS_DIR = Path(__file__).parent.parent.parent / 'config' / 'schemas'


def load_schema(schema_name: str) -> dict:
    """Loads a JSON schema from the schemas directory."""
    schema_path = SCHEMAS_DIR / schema_name
    with open(schema_path, 'r', encoding="utf-8") as f:
        return json.load(f)

def _schema_body(data: dict) -> dict:
    """Return the inner schema body if wrapped under 'schema', else the data itself."""
    if isinstance(data, dict) and 'schema' in data and isinstance(data['schema'], dict):
        return data['schema']
    return data

paper_extraction_schema = _schema_body(load_schema('paper_extraction_schema.json'))
paper_classification_schema = _schema_body(load_schema('paper_classification_schema.json'))


def _is_emptyish(val) -> bool:
    if val is None:
        return True
    if isinstance(val, str):
        return val.strip().lower() in {"", "nan", "null", "none", "n/a", "unknown"}
    return False


def _clean_scalar_string(val: object) -> str:
    s = str(val).strip()
    if not s:
        return ""
    s = s.replace("\ufeff", "")
    s = s.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    s = s.replace("\u00a0", " ")
    s = s.translate(
        str.maketrans(
            {
                "“": '"',
                "”": '"',
                "„": '"',
                "‟": '"',
                "’": "'",
                "‘": "'",
                "‛": "'",
                "′": "'",
            }
        )
    )
    s = unicodedata.normalize("NFKC", s)
    while s and s[0] in ('"', "'", '`') and not (len(s) >= 2 and s[0] == s[-1]):
        s = s[1:].lstrip()
    s = re.sub(r"[\s,]+$", "", s).strip()
    lowered = s.lower()
    if lowered in {"nan", "null", "none", "n/a", "unknown"}:
        return ""
    if lowered == "0":
        return ""
    return s


def _coerce_pipe_string(val: object) -> str:
    if _is_emptyish(val):
        return ""
    if isinstance(val, list):
        parts: list[str] = []
        for x in val:
            if isinstance(x, (str, int, float)):
                s = _clean_scalar_string(x)
                if s:
                    parts.append(s)
        return "|".join(parts)
    return _clean_scalar_string(val)


def _maybe_parse_dict_string(val: object) -> object:
    if not isinstance(val, str):
        return val
    s = _clean_scalar_string(val)
    if not s:
        return val

    if s.startswith("```"):
        lines = s.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            s = "\n".join(lines[1:-1]).strip()

    while True:
        before = s
        s = re.sub(r"^(?:[-*•]|\d+\.)\s+", "", s).lstrip()
        if s == before:
            break

    candidates = [s]
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        candidates.append(_clean_scalar_string(s[1:-1]))

    def _try_parse_from_substring(text: str) -> str | None:
        if "{" in text and "}" in text:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                return text[start : end + 1]
        return None

    def _try_parse_kv_lines(text: str) -> dict | None:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return None
        if not any(":" in ln for ln in lines):
            return None
        out: dict = {}
        for ln in lines:
            ln = re.sub(r"^(?:[-*•]|\d+\.)\s+", "", ln).strip()
            if ":" not in ln:
                continue
            k, v = ln.split(":", 1)
            k = _clean_scalar_string(k)
            v = _clean_scalar_string(v)
            if not k or not v:
                continue
            if v.lower() in {"nan", "null", "none", "n/a", "unknown"}:
                continue
            out[k] = v
        return out or None

    for cand in candidates:
        sub = _try_parse_from_substring(cand)
        parse_candidates = [cand]
        if isinstance(sub, str) and sub != cand:
            parse_candidates.insert(0, sub)

        for pc in parse_candidates:
            if pc.startswith('{') and pc.endswith('}'):
                for variant in (pc, pc.replace('\\"', '"').replace("\\'", "'")):
                    try:
                        parsed = json.loads(variant)
                        if isinstance(parsed, dict):
                            return parsed
                    except Exception:
                        pass
                try:
                    parsed = ast.literal_eval(pc)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    pass

        kv = _try_parse_kv_lines(cand)
        if isinstance(kv, dict):
            return kv
    return val

def _coerce_year(value) -> int | None:
    """Attempt to coerce a value into a 4-digit year integer.

    Accepts ints, numeric strings, or text containing a 4-digit year (e.g., citation strings).
    Returns None if no valid year can be determined.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    # Try direct int cast first
    try:
        return int(value)
    except (ValueError, TypeError):
        pass
    # Extract first 4-digit year from string (e.g., "... 2021 - ...")
    s = str(value)
    m = re.search(r'(19|20)\d{2}', s)
    if m:
        try:
            return int(m.group(0))
        except ValueError:
            return None
    return None

def validate_and_normalize_extraction(obj: dict) -> dict:
    """
    Validates and normalizes the paper extraction data against the paper_extraction_schema.json.

    Ensures all required fields are present, adding default empty values if they are missing.
    This prevents downstream errors by providing a consistently structured object.

    Args:
        obj: The dictionary produced by the 'ex_paper.md' prompt.

    Returns:
        A validated and normalized dictionary. If input is invalid, returns a valid empty shell.
    """
    if not isinstance(obj, dict):
        obj = {}

    defaults = {
        'Journal': '',
        'Abstract': '',
        'Datasets Used': [],
        'Preprocessing Methods': [],
        'Augmentation Methods': [],
        'Models Used': [],
        'Results': '',
        'Processing Pipeline': []
    }

    for key, default_value in defaults.items():
        if key not in obj or not obj[key]:
            obj[key] = default_value

    # Clean scalar strings and tolerate dict/list mistakes for scalar fields
    for key in ('Journal', 'Abstract', 'Methods', 'Results'):
        if key in obj:
            if isinstance(obj[key], (dict, list)):
                obj[key] = ''
            else:
                obj[key] = _clean_scalar_string(obj[key])

    # Further validation for nested objects if necessary
    if not isinstance(obj.get('Datasets Used'), list):
        obj['Datasets Used'] = []

    # Ensure Processing Pipeline is always a list (2D array of strings)
    pipeline = obj.get('Processing Pipeline')
    if not isinstance(pipeline, list):
        obj['Processing Pipeline'] = []
    else:
        # Best-effort normalization: keep only list-of-string pipelines
        normalized_pipelines: list[list[str]] = []
        for p in pipeline:
            if isinstance(p, list):
                steps = [str(s).strip() for s in p if isinstance(s, (str, int, float))]
                if steps:
                    normalized_pipelines.append(steps)
        obj['Processing Pipeline'] = normalized_pipelines

    return obj

def validate_and_normalize_classification(obj: dict, paper_year: int | None) -> dict:
    """
    Validates and normalizes the paper classification data against the paper_classification_schema.json.

    Fills in empty enum fields with conservative defaults and intelligently coerces the 'year' field,
    using the paper's original year as a fallback.

    Args:
        obj: The dictionary from the 'paper_classification_prompt.md' prompt.
        paper_year: The original year of the paper, used as a fallback.

    Returns:
        A validated and normalized dictionary that is compliant with the schema.
    """
    if not isinstance(obj, dict):
        obj = {}

    # Absent or empty means "the paper did not say". Do NOT substitute a plausible
    # value here -- that fabricates data indistinguishable from a real extraction.
    #
    # This used to default wsi_or_patch->'wsi', species->'human', staining_type->'h&e',
    # tissue_site/biological_domain/annotation_types->'other', task->'other' and
    # model_family->'CNN'. Because the classification prompt never asks for `task` or
    # `model_family` at all, those two were injected into 100% of records: all 3,306
    # papers in the January 2026 run carry task='other' and model_family='cnn'. The
    # genuinely extracted equivalents already exist elsewhere -- model_details.model_type
    # (cnn 34.5% / hybrid 22.7% / mil 17.1% / transformer 10.2% / graph-nn 4.3%) and the
    # step-1.7 task_classification booleans -- so both fields are dropped outright.
    for vestigial in ('task', 'model_family'):
        obj.pop(vestigial, None)

    # Only structural defaults remain: ensure the key exists so downstream code can rely
    # on its presence, with an empty value that honestly encodes "not reported".
    for key in ('wsi_or_patch', 'tissue_site', 'biological_domain', 'species',
                'staining_type', 'annotation_types', 'labels'):
        if key not in obj or obj[key] is None:
            obj[key] = ''

    # Coerce list returns for pipe-delimited categorical fields
    pipe_like_fields = {
        'biological_domain',
        'modality',
        'tissue_site',
        'species',
        'staining_type',
        'annotation_types',
        'preprocessing_methods',
        'augmentation_methods',
    }
    for field in pipe_like_fields:
        if field in obj:
            raw = _coerce_pipe_string(obj.get(field))
            parts = [p.strip().lower() for p in raw.split('|') if p.strip()]
            parts = [p for p in parts if p not in {'unknown', 'null', 'none', 'nan'}]
            obj[field] = '|'.join(parts) if parts else ''
    obj['staining_type'] = normalize_staining_type(obj.get('staining_type'))

    # Normalize scalar categorical fields to lowercase
    scalar_categorical_fields = {
        'domain',
        'wsi_or_patch',
        'tiling_strategy',
        'split_level',
        'split_method',
        'dataset_access',
        'license',
        'outcome_endpoint',
    }
    for field in scalar_categorical_fields:
        if field in obj:
            v = _clean_scalar_string(obj.get(field))
            lv = v.strip().lower() if v else ''
            obj[field] = '' if lv in {'unknown', 'null', 'none', 'nan', '0'} else lv

    # labels is a free-form pipe-separated string; keep 'na' as a valid value
    if 'labels' in obj:
        obj['labels'] = _clean_scalar_string(obj.get('labels'))

    # Parse dict-like strings for dict fields
    for dict_field in ('class_counts', 'paper', 'annotation_provenance', 'model_details', 'multimodal_flags'):
        if dict_field in obj:
            parsed = _maybe_parse_dict_string(obj.get(dict_field))
            if isinstance(parsed, dict):
                cleaned: dict = {}
                for k, v in parsed.items():
                    ck = _clean_scalar_string(k)
                    if ck:
                        cleaned[ck] = v
                obj[dict_field] = cleaned
            elif _is_emptyish(parsed):
                obj[dict_field] = {}

    # Normalize numeric fields: treat placeholder strings/zeros as missing
    for num_field in ('mpp', 'tile_size_px', 'n_patients', 'n_slides', 'n_tiles', 'n_centers'):
        if num_field in obj:
            v = obj.get(num_field)
            if isinstance(v, str):
                sv = _clean_scalar_string(v)
                if not sv:
                    obj[num_field] = None
                else:
                    try:
                        obj[num_field] = float(sv) if '.' in sv else int(float(sv))
                    except Exception:
                        obj[num_field] = None
            elif v in (0, 0.0):
                obj[num_field] = None

    if 'time_horizon_months' in obj:
        obj.pop('time_horizon_months', None)

    # Year validation and normalization
    year = _coerce_year(obj.get('year'))
    fallback_year = _coerce_year(paper_year)

    # Clamp the year to the schema's constraints if present; otherwise use sensible defaults
    try:
        min_year = int(paper_classification_schema['properties']['year']['minimum'])
        max_year = int(paper_classification_schema['properties']['year']['maximum'])
    except Exception:
        min_year = 1900
        max_year = 2100
    if year is not None:
        obj['year'] = max(min_year, min(year, max_year))
    elif fallback_year is not None:
        obj['year'] = max(min_year, min(fallback_year, max_year))
    else:
        obj['year'] = min_year # or some other default

    return obj
