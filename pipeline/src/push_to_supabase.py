"""
Push pipeline data to Supabase database.

This module handles the transformation and upload of:
- Datasets from datasets_seed_step2.json → final_datasets table
- Papers from final_extracted.json → papers_dct table
- Metrics (if available) → filtered_metrics table
"""

import os
import re
import json
import hashlib
import ast
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from dotenv import load_dotenv

from supabase import create_client, Client
from .utils import get_logger
from .paper_code.filters import text_source
from .paper_code.dedupe import PaperIndex, remove_duplicates
from .paper_code.standardize import normalize_staining_type, standardize_journal, standardize_pipe_separated_labels
import psycopg

logger = get_logger(__name__)


def canonicalize_url(value: Any) -> Optional[str]:
    url = normalize_value(value)
    if url is None:
        return None
    url = str(url).strip()
    if not url:
        return None
    url = url.split("#", 1)[0]
    url = re.sub(r"/+$", "", url)
    return url or None


def _sanitize_text(s: str) -> str:
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
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _sanitize_text_keep_newlines(s: str) -> str:
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
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s


def normalize_value(value: Any) -> Any:
    """
    Normalize a value for database insertion.
    
    Converts:
    - "nan", "NaN", "", None → NULL (None)
    - Numeric strings → numbers where appropriate
    - Strips whitespace from strings
    """
    if value is None:
        return None
    if isinstance(value, str):
        # Strip whitespace
        value = _sanitize_text(value)
        
        # Convert empty strings and "nan" to None
        if value == "" or value.lower() == "nan":
            return None
        
        # Collapse multiple spaces
        value = re.sub(r'\s+', ' ', value)
    
    return value


def to_int(value: Any) -> Optional[int]:
    """Convert value to integer, return None if invalid."""
    value = normalize_value(value)
    if value is None:
        return None
    
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


def to_float(value: Any) -> Optional[float]:
    """Convert value to float, return None if invalid."""
    value = normalize_value(value)
    if value is None:
        return None
    
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def stated(value: Optional[float]) -> Optional[float]:
    """NULL for 0: dataset numbers use 0 for "not stated" (no dataset has 0 slides or 0 mpp)."""
    return None if value == 0 else value


def to_bool(value: Any) -> Optional[bool]:
    """Convert value to boolean, return None if invalid."""
    value = normalize_value(value)
    if value is None:
        return None
    
    if isinstance(value, bool):
        return value
    
    if isinstance(value, str):
        if value.lower() in ('true', '1', 'yes'):
            return True
        if value.lower() in ('false', '0', 'no'):
            return False
    
    return None


def normalize_categorical(value: Any, lowercase: bool = True) -> Optional[str]:
    """Normalize categorical values (legacy function).

    Kept for compatibility. Prefer using ``case_categorical`` for display-friendly casing.
    """
    value = normalize_value(value)
    if value is None:
        return None
    return str(value).lower() if lowercase else str(value)


def _title_case_token(token: str) -> str:
    """Title-case a token while respecting hyphenated parts."""
    if not token:
        return token
    parts = token.split("-")
    return "-".join(p.capitalize() if p else p for p in parts)


def case_categorical(value: Any) -> Optional[str]:
    """Return a cleanly-cased categorical string with acronym preservation.

    Examples:
    - "h&e" -> "H&E"
    - "ihc" -> "IHC"
    - "ffpe" -> "FFPE"
    - "histopathology" -> "Histopathology"
    - "colon" -> "Colon"
    - "classification" -> "Classification"
    """
    v = normalize_value(value)
    if v is None:
        return None
    s = str(v).strip()
    lower = s.lower()

    # Common acronyms and special cases
    specials = {
        "wsi": "WSI",
        "ihc": "IHC",
        "h&e": "H&E",
        "he": "H&E",
        "ffpe": "FFPE",
        "cnn": "CNN",
        "rnn": "RNN",
        "mlp": "MLP",
        "ai": "AI",
        "roi": "ROI",
        "auc": "AUC",
        "auprc": "AUPRC",
    }
    if lower in specials:
        return specials[lower]

    # Title-case words while keeping small words lower if in middle
    small_words = {"and", "or", "of", "the", "in", "on", "for", "to", "with", "by"}
    words = s.split()
    cased: List[str] = []
    for i, w in enumerate(words):
        lw = w.lower()
        if lw in specials:
            cased.append(specials[lw])
        elif i != 0 and lw in small_words:
            cased.append(lw)
        else:
            cased.append(_title_case_token(lw))
    return " ".join(cased)


def standardize_pipe_separated(value: Any) -> Optional[str]:
    """Standardize a possibly pipe-separated categorical string using ``case_categorical`` per part."""
    v = normalize_value(value)
    if v is None:
        return None
    parts = [p.strip() for p in str(v).split("|")]
    out = [case_categorical(p) for p in parts if p]
    out = [p for p in out if p]
    return "|".join(out) if out else None


def join_array(arr: List[Any], sep: str = "|") -> Optional[str]:
    """Join array into pipe-separated string, filtering out empty values."""
    if not arr:
        return None
    
    # Filter out None and empty strings, convert dicts/complex types to strings
    filtered = []
    for item in arr:
        if isinstance(item, dict):
            # Skip dictionaries - they're not simple values
            continue
        elif isinstance(item, (list, tuple)):
            # Skip nested lists
            continue
        else:
            normalized = normalize_value(item)
            if normalized is not None:
                filtered.append(str(normalized))
    
    if not filtered:
        return None
    
    return sep.join(filtered)


_AUGMENTATION_ALLOWED: Set[str] = {
    "horizontal-flip",
    "vertical-flip",
    "rotation",
    "random-rotation",
    "90-rotation",
    "180-rotation",
    "random-crop",
    "center-crop",
    "color-jitter",
    "brightness",
    "contrast",
    "saturation",
    "hue",
    "gaussian-blur",
    "gaussian-noise",
    "elastic-deformation",
    "affine-transform",
    "scale",
    "shear",
    "translate",
    "cutout",
    "random-erasing",
    "mixup",
    "cutmix",
    "mosaic",
    "randaugment",
    "autoaugment",
    "stain-augmentation",
    "color-augmentation",
    "random-resize",
    "perspective-transform",
    "grid-distortion",
    "optical-distortion",
    "morphological-augmentation",
    "coarse-dropout",
    "grid-dropout",
    "channel-shuffle",
    "normalize",
    "random-flip",
    "random-brightness",
    "random-contrast",
    "random-saturation",
    "additive-noise",
    "multiplicative-noise",
    "motion-blur",
    "median-blur",
    "jpeg-compression",
    "none",
    "other",
}


_PREPROCESSING_ALLOWED: Set[str] = {
    "tissue-detection",
    "color-normalization",
    "stain-normalization",
    "macenko-normalization",
    "reinhard-normalization",
    "vahadane-normalization",
    "background-removal",
    "tiling",
    "patching",
    "resizing",
    "rescaling",
    "grayscale-conversion",
    "artifact-removal",
    "blur-detection",
    "pen-mark-removal",
    "focus-stacking",
    "white-balance",
    "clahe",
    "histogram-equalization",
    "contrast-enhancement",
    "denoising",
    "sharpening",
    "otsu-thresholding",
    "morphological-operations",
    "roi-extraction",
    "downsampling",
    "pyramid-generation",
    "z-score-normalization",
    "min-max-normalization",
    "quality-filtering",
    "center-cropping",
    "random-cropping",
    "stain-separation",
    "foreground-extraction",
    "none",
    "other",
}


def _standardize_pipe_tokens(value: Any, allowed: Set[str]) -> Optional[str]:
    if value is None:
        return None

    items: List[str] = []
    if isinstance(value, list):
        for x in value:
            if isinstance(x, (str, int, float)):
                items.append(str(x))
    elif isinstance(value, (str, int, float)):
        items.append(str(value))
    else:
        return None

    tokens: List[str] = []
    for raw in items:
        s = _sanitize_text_keep_newlines(raw)
        if not s:
            continue

        if s.startswith("```"):
            lines = s.splitlines()
            if len(lines) >= 2 and lines[-1].strip() == "```":
                s = "\n".join(lines[1:-1]).strip()

        parts = re.split(r"[|,;\n\r]+", s)
        for part in parts:
            p = _sanitize_text(part)
            if not p:
                continue

            p = re.sub(r"^(?:[-*•]|\d+\.)\s+", "", p).strip()
            p = re.sub(r"\s*\(.*?\)\s*", "", p).strip()
            p = p.strip("\"'`")

            lower = p.lower().strip()
            if not lower or lower in {"nan", "null", "n/a", "na", "unknown"}:
                continue

            token = lower.replace("_", " ")
            token = re.sub(r"\s+", "-", token)
            token = re.sub(r"[^a-z0-9-]", "", token)
            token = re.sub(r"-+", "-", token).strip("-")
            if not token:
                continue

            if token.startswith("other"):
                token = "other"

            if token not in allowed:
                continue

            tokens.append(token)

    if not tokens:
        return None

    seen: Set[str] = set()
    out: List[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)

    return "|".join(out) if out else None


_BIOLOGICAL_DOMAIN_ALLOWED: Set[str] = {
    "oncology",
    "immunology",
    "neurology",
    "cardiology",
    "nephrology",
    "hepatology",
    "pulmonology",
    "gastroenterology",
    "dermatology",
    "hematology",
    "infectious-disease",
    "transplant",
    "developmental",
    "toxicology",
    "other",
}


_BACKBONE_ALLOWED: Set[str] = {
    "resnet",
    "densenet",
    "efficientnet",
    "vgg",
    "inception",
    "vit",
    "swin",
    "convnext",
    "deit",
    "custom",
    "other",
}


_MODEL_TYPE_ALLOWED: Set[str] = {
    "cnn",
    "transformer",
    "mil",
    "graph-nn",
    "hybrid",
    "rnn",
    "other",
}


_DOMAIN_ALLOWED: Set[str] = {
    "histopathology",
    "cytology",
    "molecular-pathology",
    "optical-label-free",
    "gross-pathology",
    "multimodal",
}


_OUTCOME_ENDPOINT_ALLOWED: Set[str] = {
    "diagnosis",
    "grade",
    "stage",
    "survival",
    "recurrence",
    "treatment-response",
    "none",
    "other",
}


def _standardize_enum_token(value: Any, allowed: Set[str]) -> Optional[str]:
    if value is None:
        return None

    items: List[str] = []
    if isinstance(value, list):
        for x in value:
            if isinstance(x, (str, int, float)):
                items.append(str(x))
    elif isinstance(value, (str, int, float)):
        items.append(str(value))
    else:
        return None

    alias = {
        "dense-net": "densenet",
        "dense_net": "densenet",
        "res-net": "resnet",
        "res_net": "resnet",
        "vggnet": "vgg",
        "efficient-net": "efficientnet",
        "efficient_net": "efficientnet",
        "conv-next": "convnext",
        "conv_next": "convnext",
        "vision-transformer": "vit",
        "swin-transformer": "swin",
        "graphnn": "graph-nn",
        "graph_nn": "graph-nn",
    }

    for raw in items:
        s = _sanitize_text_keep_newlines(raw)
        if not s:
            continue

        if s.startswith("```"):
            lines = s.splitlines()
            if len(lines) >= 2 and lines[-1].strip() == "```":
                s = "\n".join(lines[1:-1]).strip()

        parts = re.split(r"[|,;\n\r]+", s)
        for part in parts:
            p = _sanitize_text(part)
            if not p:
                continue
            p = re.sub(r"^(?:[-*•]|\d+\.)\s+", "", p).strip()
            p = re.sub(r"\s*\(.*?\)\s*", "", p).strip()
            p = p.strip("\"'`")

            lower = p.lower().strip()
            if not lower or lower in {"nan", "null", "n/a", "na", "unknown"}:
                continue

            token = lower.replace("_", " ")
            token = re.sub(r"\s+", "-", token)
            token = re.sub(r"[^a-z0-9-]", "", token)
            token = re.sub(r"-+", "-", token).strip("-")
            if not token:
                continue

            token = alias.get(token, token)
            if token in allowed:
                return token

            if token.startswith("other") and "other" in allowed:
                return "other"

    return None


_ANNOTATION_TYPES_ALLOWED: Set[str] = {
    "polygon",
    "mask",
    "point",
    "box",
    "label",
    "landmark-points",
    "roi-crop",
    "heatmap",
    "cell-graph",
    "text",
    "other",
}


_MODALITY_ALLOWED: Set[str] = {
    "brightfield",
    "fluorescence",
    "ihc-dab",
    "multiplex-if",
    "fish",
    "srh",
    "oct",
    "mpm",
    "tma",
    "spatial-transcriptomics",
    "other",
}


_TISSUE_SITE_ALLOWED: Set[str] = {
    "kidney",
    "breast",
    "lung",
    "brain",
    "skin",
    "blood",
    "multi-organ",
    "rectum",
    "colon",
    "colorectal",
    "gastrointestinal",
    "stomach",
    "gastric",
    "uterus",
    "endometrial",
    "lymph-node",
    "placenta",
    "bone-marrow",
    "cervix",
    "esophagus",
    "ovary",
    "liver",
    "prostate",
    "eye",
    "head-neck",
    "oral-cavity",
    "pancreas",
    "bone",
    "bladder",
    "thyroid",
    "adrenal",
    "spleen",
    "heart",
    "muscle",
    "testis",
    "other",
}


def _clean_enumish_token(raw: str) -> Optional[str]:
    s = _sanitize_text(raw)
    if not s:
        return None
    s = re.sub(r"^(?:[-*•]|\d+\.)\s+", "", s).strip()
    s = re.sub(r"\s*\(.*?\)\s*", "", s).strip()
    s = s.strip("\"'`")
    s = s.strip()
    lower = s.lower().strip()
    if not lower or lower in {"nan", "null", "n/a", "na", "unknown", "none", "0"}:
        return None
    token = lower.replace("_", " ")
    token = re.sub(r"\s+", "-", token)
    token = re.sub(r"[^a-z0-9&-]", "", token)
    token = re.sub(r"-+", "-", token).strip("-")
    if not token:
        return None

    if token in {"he", "h-e", "h-and-e", "hande", "h& e", "h &e", "h & e"}:
        return "h&e"
    if token in {"ihc dab", "ihc-dab", "ihc/dab", "ihcdab"}:
        return "ihc-dab"
    if token in {"multiplex if", "multiplex-if", "multiplexif"}:
        return "multiplex-if"
    if token in {"landmark points", "landmark-points", "landmarkpoints"}:
        return "landmark-points"
    if token in {"roi crop", "roi-crop", "roicrop"}:
        return "roi-crop"
    if token in {"cell graph", "cell-graph", "cellgraph"}:
        return "cell-graph"
    if token in {"head & neck", "head-neck", "headneck"}:
        return "head-neck"
    if token in {"oral cavity", "oral-cavity", "oralcavity"}:
        return "oral-cavity"
    if token in {"bone marrow", "bone-marrow", "bonemarrow"}:
        return "bone-marrow"
    if token in {"lymph node", "lymph-node", "lymphnode"}:
        return "lymph-node"
    if token in {"multi organ", "multi-organ", "multiorgan"}:
        return "multi-organ"
    if token in {"spatial transcriptomics", "spatial-transcriptomics"}:
        return "spatial-transcriptomics"
    return token


def _standardize_pipe_enums(value: Any, allowed: Set[str]) -> Optional[str]:
    if value is None:
        return None

    raw_parts: List[str] = []
    if isinstance(value, list):
        for item in value:
            if item is None:
                continue
            if isinstance(item, (str, int, float)) and not isinstance(item, bool):
                raw_parts.append(str(item))
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        raw_parts.append(str(value))
    else:
        return None

    tokens: List[str] = []
    for raw in raw_parts:
        s = _sanitize_text_keep_newlines(raw)
        if not s:
            continue
        for part in re.split(r"[|,;\n\r]+", s):
            t = _clean_enumish_token(part)
            if not t:
                continue
            if t.startswith("other"):
                t = "other"
            if t in allowed:
                tokens.append(t)

    if not tokens:
        return None

    seen: Set[str] = set()
    out: List[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)

    return "|".join(out) if out else None


def clean_dataset_row(ds: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transform a dataset from JSON to database row format.
    
    Args:
        ds: Dataset dictionary from datasets_seed_step2.json
    """
    # Handle paper object
    paper = ds.get('paper')
    paper_doi = None
    paper_venue = None
    if paper and isinstance(paper, dict):
        paper_doi = normalize_value(paper.get('doi'))
        paper_venue = normalize_value(paper.get('venue'))
    
    return {
        'slug': normalize_value(ds.get('slug')),
        'dataset_name': normalize_value(ds.get('dataset_name')),
        'year': stated(to_int(ds.get('year'))),
        'description': normalize_value(ds.get('description')),
        'code_url': normalize_value(ds.get('code_url')),
        'download_url': normalize_value(ds.get('download_url')),
        'home_page_url': normalize_value(ds.get('home_page_url')),
        'institutions': normalize_value(ds.get('institutions')),
        'license': case_categorical(ds.get('license')),
        'dataset_access': case_categorical(ds.get('dataset_access')),
        'is_public': to_bool(ds.get('is_public')),
        'wsi_or_patch': case_categorical(ds.get('wsi_or_patch')),
        'tissue_site': _standardize_pipe_enums(ds.get('tissue_site'), _TISSUE_SITE_ALLOWED),
        'domain': case_categorical(ds.get('domain')),
        'modality': _standardize_pipe_enums(ds.get('modality'), _MODALITY_ALLOWED),
        'species': standardize_pipe_separated(ds.get('species')),
        'staining_type': normalize_staining_type(ds.get('staining_type')) or None,
        'sample_prep': standardize_pipe_separated(ds.get('sample_prep')),
        'task': standardize_pipe_separated(ds.get('task')),
        'annotation_level': standardize_pipe_separated(ds.get('annotation_level')),
        'annotation_types': _standardize_pipe_enums(ds.get('annotation_types'), _ANNOTATION_TYPES_ALLOWED),
        'annotation_format': standardize_pipe_separated(ds.get('annotation_format')),
        'labels': standardize_pipe_separated(ds.get('labels')),
        'mag_min': stated(to_int(ds.get('mag_min'))),
        'mag_max': stated(to_int(ds.get('mag_max'))),
        'mpp': stated(to_float(ds.get('mpp'))),
        'scanner': normalize_value(ds.get('scanner')),
        'tile_size_px': stated(to_int(ds.get('tile_size_px'))),
        'tiling_strategy': case_categorical(ds.get('tiling_strategy')),
        'image_format': standardize_pipe_separated(ds.get('image_format')),
        'approx_size_gb': stated(to_float(ds.get('approx_size_gb'))),
        'n_slides': stated(to_int(ds.get('n_slides'))),
        'n_patients': stated(to_int(ds.get('n_patients'))),
        'n_tiles': stated(to_int(ds.get('n_tiles'))),
        'n_centers': stated(to_int(ds.get('n_centers'))),
        'split_level': case_categorical(ds.get('split_level')),
        'split_method': case_categorical(ds.get('split_method')),
        'external_validation': to_bool(ds.get('external_validation')),
        'resource_urls': normalize_value(ds.get('resource_urls')),
        'paper_doi': paper_doi,
        'paper_venue': paper_venue,
        # Slug of the dataset this one is a subset of (TCGA-KICH -> TCGA-RCC), or NULL.
        'parent_slug': normalize_value(ds.get('parent_slug')),
    }


def extract_year_from_string(year_str: Any) -> Optional[int]:
    """Extract 4-digit year from various formats."""
    if year_str is None:
        return None
    
    year_str = str(year_str)
    
    # Try direct conversion first
    year = to_int(year_str)
    if year and 1900 <= year <= 2100:
        return year
    
    # Try to find 4-digit year in string
    match = re.search(r'\b(19|20)\d{2}\b', year_str)
    if match:
        return int(match.group())
    
    return None


def safe_extract(value: Any, default: Any = None) -> Any:
    """
    Safely extract data that might be a list or dict.
    
    If the value is a list, return the first element.
    If the value is a dict, return it as-is.
    Otherwise, return the default.
    
    Args:
        value: The value to extract (could be list, dict, or None)
        default: Default value to return if extraction fails
    
    Returns:
        Extracted value or default
    """
    if value is None:
        return default

    if isinstance(value, list):
        # If it's a list, try to get the first element
        if len(value) > 0:
            return value[0]
        return default

    if isinstance(value, dict):
        return value

    return default


def _maybe_parse_dict_string(val: Any) -> Any:
    if not isinstance(val, str):
        return val

    s = _sanitize_text(val)
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
        candidates.append(_sanitize_text(s[1:-1]))

    def _try_parse_from_substring(text: str) -> Any:
        if "{" in text and "}" in text:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                return text[start : end + 1]
        return None

    def _try_parse_kv_lines(text: str) -> Optional[dict]:
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
            k = _sanitize_text(k)
            v = _sanitize_text(v)
            if not k:
                continue
            if v.lower() in {"", "nan", "null", "none", "n/a", "unknown"}:
                continue
            try:
                num = float(v)
                out[k] = int(num) if num.is_integer() else num
            except Exception:
                out[k] = v
        return out or None

    for cand in candidates:
        sub = _try_parse_from_substring(cand)
        parse_candidates = [cand]
        if isinstance(sub, str) and sub != cand:
            parse_candidates.insert(0, sub)

        for pc in parse_candidates:
            pc = pc.strip()
            if pc.startswith("{") and pc.endswith("}"):
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


def build_paper_row(paper: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transform a paper from JSON to database row format.
    
    Args:
        paper: Paper dictionary from final_extracted.json
    """
    extraction = paper.get('extraction', {})
    sum_paper = safe_extract(extraction.get('sum_paper.md'), {})
    # Nested paper_ex_json inside sum_paper (some extractions put data here)
    paper_ex_json = sum_paper.get('paper_ex_json', {}) or {}
    # Primary schema-aligned paper extraction block (Journal, Abstract, Methods, Results)
    ex_paper = safe_extract(extraction.get('ex_paper.md'), {})
    classification = safe_extract(extraction.get('paper_classification_prompt.md'), {})
    dataset_details = safe_extract(extraction.get('dataset_details.md'), {})
    
    # Extract URL (primary key)
    url = canonicalize_url(paper.get('link'))
    
    # Extract title (prefer extraction)
    title = normalize_value(sum_paper.get('title') or paper.get('title'))
    
    # Extract authors
    authors = None
    if sum_paper.get('authors'):
        if isinstance(sum_paper['authors'], list):
            authors = join_array(sum_paper['authors'], sep=", ")
        else:
            authors = normalize_value(sum_paper['authors'])
    elif paper.get('authors'):
        authors = normalize_value(paper['authors'])
    
    # Extract year
    year = None
    if sum_paper.get('year'):
        year = extract_year_from_string(sum_paper['year'])
    if not year and paper.get('year'):
        year = extract_year_from_string(paper['year'])
    
    # Extract journal/venue and standardize (check nested paper_ex_json as fallback)
    journal_raw = normalize_value(
        sum_paper.get('publication_venue')
        or sum_paper.get('Journal')
        or sum_paper.get('journal')
        or paper_ex_json.get('journal')
        or paper_ex_json.get('Journal')
        or ex_paper.get('Journal')
    )
    journal = standardize_journal(journal_raw)
    
    # Extract abstract (sum_paper.md is canonical source per paper_extraction_schema.json)
    abstract = normalize_value(
        sum_paper.get('Abstract')
        or sum_paper.get('abstract')
        or paper_ex_json.get('Abstract')
        or paper_ex_json.get('abstract')
        or ex_paper.get('Abstract')
    )
    
    # Extract methods (sum_paper.md is canonical source per paper_extraction_schema.json)
    methods = normalize_value(
        sum_paper.get('Methods')
        or sum_paper.get('methods')
        or paper_ex_json.get('Methods')
        or paper_ex_json.get('methods')
        or ex_paper.get('Methods')
    )
    
    # Extract datasets used
    datasets_used = None
    if dataset_details and dataset_details.get('datasets'):
        slugs = [d.get('slug') for d in dataset_details['datasets'] if d.get('slug')]
        datasets_used = join_array(slugs)
    # Non-public cohorts the paper uses: no catalog entry can be linked for them.
    private = [d for d in ((dataset_details or {}).get('private_datasets') or [])
               if isinstance(d, dict) and str(d.get('name') or '').strip()]
    private_datasets = join_array([' '.join(str(d['name']).split()) for d in private])
    
    # Extract results (sum_paper.md is canonical source per paper_extraction_schema.json)
    results = normalize_value(
        sum_paper.get('Results')
        or sum_paper.get('results')
        or paper_ex_json.get('Results')
        or paper_ex_json.get('results')
        or ex_paper.get('Results')
    )
    
    # Extract high-level processing pipeline(s) (2D list of step labels, sum_paper.md is canonical source)
    processing_pipeline = (
        sum_paper.get('Processing Pipeline')
        or paper_ex_json.get('Processing Pipeline')
        or ex_paper.get('Processing Pipeline')
        or None
    )
    
    # Extract classification fields
    wsi_or_patch = case_categorical(classification.get('wsi_or_patch'))
    tissue_site = _standardize_pipe_enums(classification.get('tissue_site'), _TISSUE_SITE_ALLOWED)
    domain = _standardize_enum_token(classification.get('domain'), _DOMAIN_ALLOWED)
    modality = _standardize_pipe_enums(classification.get('modality'), _MODALITY_ALLOWED)
    species = standardize_pipe_separated(classification.get('species'))
    staining_type = normalize_staining_type(classification.get('staining_type')) or None

    # Derive task from task_classification booleans when available.
    # This prefers the dedicated task classification step over the legacy
    # string field from paper_classification_prompt.md.
    task_flags = paper.get('task_classification') or {}
    task: Optional[str] = None
    if isinstance(task_flags, dict) and task_flags:
        active_tasks = [name for name, flag in task_flags.items() if flag]
        if active_tasks:
            task = standardize_pipe_separated("|".join(active_tasks))
    # No fallback to classification['task']: the classification prompt never asked for
    # that field, so it only ever held the validator's injected 'other' default. If the
    # step-1.7 task classification is missing, leave task NULL rather than assert 'other'.
    annotation_types = _standardize_pipe_enums(classification.get('annotation_types'), _ANNOTATION_TYPES_ALLOWED)
    labels = standardize_pipe_separated_labels(normalize_value(classification.get('labels')))
    
    # Extract technical fields
    mpp = to_float(classification.get('mpp'))
    scanner = normalize_value(classification.get('scanner'))
    tile_size_px = to_int(classification.get('tile_size_px'))
    tiling_strategy = case_categorical(classification.get('tiling_strategy'))
    
    # Extract counts
    n_patients = to_int(classification.get('n_patients'))
    n_slides = to_int(classification.get('n_slides'))
    n_tiles = to_int(classification.get('n_tiles'))
    n_centers = to_int(classification.get('n_centers'))
    
    # Extract class_counts (jsonb object)
    class_counts = None
    if classification.get('class_counts'):
        cc = _maybe_parse_dict_string(classification.get('class_counts'))
        if isinstance(cc, dict):
            cleaned_cc: dict = {}
            for k, v in cc.items():
                ck = normalize_value(k)
                if not isinstance(ck, str) or not ck.strip():
                    continue
                if isinstance(v, str):
                    sv = _sanitize_text(v)
                    if not sv or sv.lower() in {"nan", "null", "none", "n/a", "unknown"}:
                        continue
                    try:
                        num = float(sv)
                        cleaned_cc[ck] = int(num) if num.is_integer() else num
                        continue
                    except Exception:
                        cleaned_cc[ck] = sv
                        continue
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    cleaned_cc[ck] = int(v) if isinstance(v, float) and float(v).is_integer() else v
                    continue
                cleaned_cc[ck] = v
            class_counts = cleaned_cc or None

    # Extract split information
    split_level = case_categorical(classification.get('split_level'))
    split_method = case_categorical(classification.get('split_method'))
    external_validation = to_bool(classification.get('external_validation'))
    
    # Extract annotation_provenance object
    annotation_provenance = classification.get('annotation_provenance')
    n_annotators = None
    annotator_expertise = None
    annotator_consensus = None
    if annotation_provenance and isinstance(annotation_provenance, dict):
        n_annotators = to_int(annotation_provenance.get('n_annotators'))
        annotator_expertise = case_categorical(annotation_provenance.get('expertise'))
        annotator_consensus = case_categorical(annotation_provenance.get('consensus'))
    
    # Extract outcome information
    outcome_endpoint = _standardize_enum_token(classification.get('outcome_endpoint'), _OUTCOME_ENDPOINT_ALLOWED)

    # Extract model_details object from classification (schema-aligned)
    model_details_obj = classification.get('model_details')
    model_type = None
    backbone = None
    pretraining_strategy = None
    foundation_model = None
    if model_details_obj and isinstance(model_details_obj, dict):
        model_type = _standardize_enum_token(model_details_obj.get('model_type'), _MODEL_TYPE_ALLOWED)
        backbone = _standardize_enum_token(model_details_obj.get('backbone'), _BACKBONE_ALLOWED)
        pretraining_strategy = case_categorical(model_details_obj.get('pretraining_strategy'))
        foundation_model = case_categorical(model_details_obj.get('foundation_model'))

    # Extract multimodal_flags object
    multimodal_flags = classification.get('multimodal_flags')
    uses_radiology = None
    other_modalities = None
    if multimodal_flags and isinstance(multimodal_flags, dict):
        uses_radiology = to_bool(multimodal_flags.get('uses_radiology'))
        other_modalities = normalize_value(multimodal_flags.get('other_modalities'))
    
    # Extract access and license
    dataset_access = case_categorical(classification.get('dataset_access'))
    license_type = case_categorical(classification.get('license'))
    resource_urls = normalize_value(classification.get('resource_urls'))
    
    # Extract paper metadata object
    paper_obj = classification.get('paper')
    paper_doi = None
    paper_venue = None
    if paper_obj and isinstance(paper_obj, dict):
        paper_doi = normalize_value(paper_obj.get('doi'))
        paper_venue = normalize_value(paper_obj.get('venue'))
    
    # Extract preprocessing_methods and augmentation_methods from classification
    preprocessing_methods = _standardize_pipe_tokens(classification.get('preprocessing_methods'), _PREPROCESSING_ALLOWED)
    augmentation_methods = _standardize_pipe_tokens(classification.get('augmentation_methods'), _AUGMENTATION_ALLOWED)
    
    # Derive model_family from the genuinely-extracted model_details.model_type.
    # The classification prompt never asked for a `model_family` field, so the old
    # `classification.get('model_family')` only ever saw the validator's injected
    # default -- it was 'cnn' for 100% of the January 2026 corpus. model_type carries
    # real variance (cnn / hybrid / mil / transformer / graph-nn) and is pipe-capable.
    model_details = classification.get('model_details')
    model_family = None
    if isinstance(model_details, dict):
        model_family = standardize_pipe_separated(normalize_value(model_details.get('model_type')))
    
    # Extract biological_domain from classification
    biological_domain = _standardize_pipe_tokens(classification.get('biological_domain'), _BIOLOGICAL_DOMAIN_ALLOWED)
    
    return {
        'url': url,
        'title': title,
        'authors': authors,
        'year': year,
        'journal': journal,
        'citations': to_int(paper.get('citations') or paper.get('scholar_metadata', {}).get('citations')),
        'abstract': abstract,
        'methods': methods,
        'datasets_used': datasets_used,
        # None for extractions made before private_datasets existed: unknown, not "no".
        'uses_private_data': bool(private) if 'private_datasets' in (dataset_details or {}) else None,
        'private_datasets': private_datasets,
        'preprocessing_methods': preprocessing_methods,
        'augmentation_methods': augmentation_methods,
        'processing_pipeline': processing_pipeline,
        'model_family': model_family,
        'results': results,
        'wsi_or_patch': wsi_or_patch,
        'tissue_site': tissue_site,
        'domain': domain,
        'biological_domain': biological_domain,
        'modality': modality,
        'species': species,
        'staining_type': staining_type,
        # What the extraction read: full_text, abstract (landing page only), partial, ...
        'text_source': text_source(paper.get('fulltext') or ''),
        'task': task,
        'annotation_types': annotation_types,
        'labels': labels,
        'mpp': mpp,
        'scanner': scanner,
        'tile_size_px': tile_size_px,
        'tiling_strategy': tiling_strategy,
        'n_patients': n_patients,
        'n_slides': n_slides,
        'n_tiles': n_tiles,
        'n_centers': n_centers,
        'class_counts': class_counts,
        'split_level': split_level,
        'split_method': split_method,
        'external_validation': external_validation,
        'n_annotators': n_annotators,
        'annotator_expertise': annotator_expertise,
        'annotator_consensus': annotator_consensus,
        'outcome_endpoint': outcome_endpoint,
        'model_type': model_type,
        'backbone': backbone,
        'pretraining_strategy': pretraining_strategy,
        'foundation_model': foundation_model,
        'uses_radiology': uses_radiology,
        'other_modalities': other_modalities,
        'dataset_access': dataset_access,
        'license': license_type,
        'resource_urls': resource_urls,
        'paper_doi': paper_doi,
        'paper_venue': paper_venue,
    }


def _slug_forms(name: str) -> Set[str]:
    """Every slug a dataset name may have been stored under (2.2 kept spaces-to-dashes,
    the canonical catalog strips punctuation)."""
    n = (name or "").strip()
    simple = n.lower().replace(' ', '-').replace('_', '-')
    canon = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", n.lower())).strip("-")
    return {x for x in (n, simple, canon) if x}


def resolve_metric_dataset(reported: str, linked: List[str]) -> Optional[str]:
    """Which of the paper's linked catalog datasets a metric result is for.

    `reported` is the dataset as the paper names it in the result; `linked` are the
    catalog slugs step 2.1 linked to the paper. Returns None when the result is on a
    dataset outside the catalog (a private cohort) or cannot be told apart -- the
    row is then stored as a paper-level result instead of being copied onto every
    linked dataset, which is what the old one-dict-per-task format did.
    """
    if not linked:
        return None
    if not (reported or "").strip():
        return linked[0] if len(linked) == 1 else None
    rep_forms = _slug_forms(reported)
    rep_canon = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", reported.lower())).strip("-")
    for slug in linked:
        if rep_forms & _slug_forms(slug):
            return slug
    hits = [slug for slug in linked
            if len(slug) >= 4 and (slug in rep_canon or (len(rep_canon) >= 4 and rep_canon in slug))]
    return hits[0] if len(hits) == 1 else None


def metric_key(paper_url: str, dataset_slug: Optional[str], task: str, result: Dict[str, Any]) -> str:
    """Primary key of a filtered_metrics row: one row per paper, dataset, task, split,
    evaluation level, subset (class or subgroup) and method variant (when named)."""
    parts = [paper_url or "", dataset_slug or "", (result.get('dataset') or "").lower(), task or "",
             result.get('split') or "", result.get('level') or "", (result.get('subset') or "").lower()]
    if result.get('method'):  # rows without one keep the keys they had
        parts.append(result['method'].lower())
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def build_metric_row(
    paper_url: str,
    dataset_slug: Optional[str],
    task: str,
    metrics: Dict[str, Any],
    result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build a single row for the filtered_metrics table.
    
    Args:
        paper_url: URL of the paper
        dataset_slug: Slug of the dataset (can be None for paper-level metrics)
        task: Task type (classification/segmentation/detection/regression/survival)
        metrics: Dict of metric values
    
    Returns:
        Row dict ready for Supabase
    """
    # NOTE: Do not synthesize a fake dataset_slug (e.g., "paper-level").
    # The filtered_metrics.dataset_slug column has a foreign key to final_datasets.slug,
    # and Supabase/Postgres allow NULL for this FK. For true paper-level metrics
    # that are not tied to a specific dataset, we therefore leave dataset_slug as None.
    # Any non-NULL dataset_slug must correspond to an actual dataset row.

    result = result or {}
    row = {
        'paper_url': canonicalize_url(paper_url),
        'dataset_slug': dataset_slug,
        'task': normalize_categorical(task) if task else None,
        # Which evaluation this row is (records from the per-dataset/per-split format).
        'dataset_reported': normalize_value(result.get('dataset')),
        'split': normalize_value(result.get('split')),
        'eval_level': normalize_value(result.get('level')),
        'subset': normalize_value(result.get('subset')),
        'is_primary': bool(result.get('is_primary', True)),
        'text_source': normalize_value(result.get('text_source')),  # full_text | abstract | ...
        # Where the numbers come from (metrics.md): the method variant, the evaluation set's
        # size as stated, its CI or ± as written, and the table/row or sentence.
        # Text as written: normalize_value would make an n of "129" a number.
        **{col: " ".join(str(result.get(k) or "").split()) or None
           for col, k in (('method', 'method'), ('n_eval', 'n'), ('uncertainty', 'uncertainty'),
                          ('evidence', 'evidence'))},
    }
    row['metric_key'] = metric_key(row['paper_url'], dataset_slug, row['task'], result)
    
    # Classification metrics (all string values in schema, convert to float for DB)
    row['accuracy'] = to_float(metrics.get('accuracy'))
    row['f1_score'] = to_float(metrics.get('f1_score'))
    row['precision'] = to_float(metrics.get('precision'))
    row['recall'] = to_float(metrics.get('recall'))
    row['specificity'] = to_float(metrics.get('specificity'))
    row['npv'] = to_float(metrics.get('npv'))
    row['auc_roc'] = to_float(metrics.get('auc_roc'))
    row['auprc'] = to_float(metrics.get('auprc'))
    row['balanced_accuracy'] = to_float(metrics.get('balanced_accuracy'))
    row['mcc'] = to_float(metrics.get('mcc'))
    row['kappa'] = to_float(metrics.get('kappa'))
    row['macro_f1'] = to_float(metrics.get('macro_f1'))
    row['micro_f1'] = to_float(metrics.get('micro_f1'))
    row['weighted_f1'] = to_float(metrics.get('weighted_f1'))
    
    # Segmentation metrics
    row['dice_coefficient'] = to_float(metrics.get('dice_coefficient'))
    row['iou'] = to_float(metrics.get('iou'))
    row['pixel_accuracy'] = to_float(metrics.get('pixel_accuracy'))
    row['hd95'] = to_float(metrics.get('hd95'))
    row['aji'] = to_float(metrics.get('aji'))
    
    # Detection metrics
    row['map'] = to_float(metrics.get('map'))
    row['ap50'] = to_float(metrics.get('ap50'))
    row['ap75'] = to_float(metrics.get('ap75'))
    row['froc'] = to_float(metrics.get('froc'))
    row['sensitivity_at_1_fp_per_image'] = to_float(metrics.get('sensitivity_at_1_fp_per_image'))
    row['sensitivity_at_2_fp_per_image'] = to_float(metrics.get('sensitivity_at_2_fp_per_image'))
    
    # Regression metrics
    row['mae'] = to_float(metrics.get('mae'))
    row['rmse'] = to_float(metrics.get('rmse'))
    row['r2'] = to_float(metrics.get('r2'))
    row['spearman_r'] = to_float(metrics.get('spearman_r'))
    row['pearson_r'] = to_float(metrics.get('pearson_r'))
    
    # Survival metrics
    row['c_index'] = to_float(metrics.get('c_index'))
    row['hazard_ratio'] = to_float(metrics.get('hazard_ratio'))
    row['time_dependent_auc'] = normalize_value(metrics.get('time_dependent_auc'))  # May contain labels like "AUC@1yr=0.74"
    row['ibs'] = to_float(metrics.get('ibs'))
    
    return row


def _strip_nul(value: Any) -> Any:
    """Drop NUL characters, which Postgres text and JSONB reject ('\\u0000 cannot be
    converted to text'); PDF text extraction leaves them in abstracts."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {k: _strip_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_nul(v) for v in value]
    return value


# Rows the database refused in this push, per table: push_to_supabase reports them and
# `cli push` fails on any. upsert_batch never raises, because a raise inside a caller's
# per-row try skipped its `batch = []`, so one bad row failed every later batch (the
# 2026-10-31 push lost every paper after the first paper with n_tiles over INTEGER).
FAILED_ROWS: Dict[str, List[Dict[str, Any]]] = {}
UPSERT_RETRY_WAITS = (5, 30, 120)


def _is_transient(e: Exception) -> bool:
    """A Postgres error carries a five-character SQLSTATE (22003, 23503): retrying the same
    rows gets the same answer. Anything else (Cloudflare's 520 "JSON could not be generated",
    a 5xx, a dropped connection) is worth retrying."""
    code = getattr(e, "code", None)
    return not (isinstance(code, str) and len(code) == 5)


def upsert_batch(
    supabase: Client,
    table: str,
    rows: List[Dict[str, Any]],
    dry_run: bool = False
) -> int:
    """
    Upsert a batch of rows to a Supabase table.

    A transient failure is retried after UPSERT_RETRY_WAITS. A batch the database refuses
    is split in halves until the refused rows are isolated, so the rest still land; those
    rows go to FAILED_ROWS.

    Args:
        supabase: Supabase client
        table: Table name
        rows: List of row dictionaries
        dry_run: If True, only log what would be inserted

    Returns:
        Number of rows upserted
    """
    if not rows:
        return 0
    rows = [_strip_nul(r) for r in rows]

    if dry_run:
        logger.info(f"[DRY RUN] Would upsert {len(rows)} rows to {table}")
        logger.debug(f"Sample row: {json.dumps(rows[0], indent=2)}")
        return len(rows)

    import time
    for attempt in range(len(UPSERT_RETRY_WAITS) + 1):
        try:
            supabase.table(table).upsert(rows).execute()
            logger.info(f"Upserted {len(rows)} rows to {table}")
            return len(rows)
        except Exception as e:  # noqa: BLE001 - recorded in FAILED_ROWS, never lost
            error = e
            if not _is_transient(e) or attempt == len(UPSERT_RETRY_WAITS):
                break
            logger.warning(f"Upsert of {len(rows)} rows to {table} failed ({str(e)[:200]}); "
                           f"retrying in {UPSERT_RETRY_WAITS[attempt]} s")
            time.sleep(UPSERT_RETRY_WAITS[attempt])

    if len(rows) > 1 and not _is_transient(error):
        mid = len(rows) // 2
        return (upsert_batch(supabase, table, rows[:mid], dry_run)
                + upsert_batch(supabase, table, rows[mid:], dry_run))
    for row in rows:
        key = row.get('slug') or row.get('url') or row.get('metric_key')
        logger.error(f"Error upserting to {table} ({key}): {error}")
        FAILED_ROWS.setdefault(table, []).append({"key": key, "error": str(error)[:500]})
    return 0


def init_database(db_url: str, schema_sql_path: Optional[str] = None) -> None:
    """
    Initialize the Supabase Postgres database by creating required tables and indexes.

    Args:
        db_url: The full Postgres connection URL for the Supabase database.
        schema_sql_path: SQL schema to run; defaults to supabase/migrations/0001_init.sql.
    """
    logger.info("Initializing database schema (creating tables if missing)...")
    if schema_sql_path is None:
        schema_sql_path = Path(__file__).resolve().parents[2] / "supabase" / "migrations" / "0001_init.sql"
    sql = Path(schema_sql_path).read_text(encoding="utf-8")

    # Execute SQL statements; psycopg can run a script with multiple statements
    # Supabase requires connecting to the primary database.
    try:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
        logger.info("Database schema initialization complete.")
    except Exception as e:
        error_msg = str(e)
        # Check if the error is about tables already existing
        if "already exists" in error_msg.lower():
            logger.info("Database tables already exist, skipping initialization.")
        else:
            # Re-raise other errors
            raise


def push_datasets(
    supabase: Client,
    datasets_json_path: str,
    batch_size: int = 500,
    dry_run: bool = False
) -> int:
    """
    Push datasets from JSON to Supabase.
    
    Args:
        supabase: Supabase client
        datasets_json_path: Path to datasets_seed_step2.json
        batch_size: Number of rows per batch
        dry_run: If True, only log what would be inserted
    
    Returns:
        Total number of datasets processed
    """
    logger.info(f"Loading datasets from {datasets_json_path}")
    
    with open(datasets_json_path, 'r', encoding="utf-8") as f:
        data = json.load(f)
    
    datasets = data.get('datasets', [])
    logger.info(f"Found {len(datasets)} datasets")
    
    # Deduplicate by slug
    seen_slugs = set()
    unique_datasets = []
    for ds in datasets:
        slug = ds.get('slug')
        if slug and slug not in seen_slugs:
            seen_slugs.add(slug)
            unique_datasets.append(ds)
        elif slug:
            logger.warning(f"Duplicate slug found: {slug}")
    
    logger.info(f"Processing {len(unique_datasets)} unique datasets")
    
    # Transform and batch
    total = 0
    batch = []
    
    for ds in unique_datasets:
        try:
            row = clean_dataset_row(ds)
            
            # Validate required fields
            if not row.get('slug'):
                logger.warning(f"Skipping dataset with missing slug: {ds.get('dataset_name')}")
                continue
            
            batch.append(row)
            
            if len(batch) >= batch_size:
                total += upsert_batch(supabase, 'final_datasets', batch, dry_run)
                batch = []
        
        except Exception as e:
            logger.error(f"Error processing dataset {ds.get('slug')}: {e}")
            continue
    
    # Upload remaining batch
    if batch:
        total += upsert_batch(supabase, 'final_datasets', batch, dry_run)
    
    logger.info(f"Completed dataset push: {total} datasets")
    return total


def push_papers(
    supabase: Client,
    papers_json_path: str,
    batch_size: int = 500,
    dry_run: bool = False
) -> int:
    """
    Push papers from JSON to Supabase.
    
    Args:
        supabase: Supabase client
        papers_json_path: Path to final_extracted.json
        batch_size: Number of rows per batch
        dry_run: If True, only log what would be inserted
    
    Returns:
        Total number of papers processed
    """
    logger.info(f"Loading papers from {papers_json_path}")
    
    with open(papers_json_path, 'r', encoding="utf-8") as f:
        papers = json.load(f)
    # One row per paper even when a run left two records of it under different URLs
    # (PaperIndex); push_to_supabase points the other record's metrics at the kept row.
    before = len(papers)
    papers = remove_duplicates(papers)
    if len(papers) < before:
        logger.info(f"{before - len(papers)} paper records were another record of the same paper; merged")

    # Optionally load task classification results to drive the paper-level
    # task field from the dedicated task_classification step.
    task_classified_path = Path(papers_json_path).parent / "task_classified.json"
    task_by_url: Dict[str, Dict[str, Any]] = {}
    if task_classified_path.exists():
        try:
            with open(task_classified_path, 'r', encoding="utf-8") as tf:
                task_classified = json.load(tf)
            for rec in task_classified:
                url_key = canonicalize_url(rec.get('link'))
                if not url_key:
                    continue
                flags = rec.get('task_classification') or {}
                if isinstance(flags, dict):
                    task_by_url[url_key] = flags
        except Exception as e:
            logger.error(f"Error loading task_classified.json from {task_classified_path}: {e}")
    
    logger.info(f"Found {len(papers)} papers")
    
    # Transform and batch
    total = 0
    batch = []
    
    for paper in papers:
        try:
            url_key = canonicalize_url(paper.get('link'))
            if url_key and url_key in task_by_url:
                paper['task_classification'] = task_by_url[url_key]

            row = build_paper_row(paper)
            
            # Validate required fields
            if not row.get('url'):
                logger.warning(f"Skipping paper with missing URL: {paper.get('title')}")
                continue
            
            batch.append(row)
            
            if len(batch) >= batch_size:
                total += upsert_batch(supabase, 'papers_dct', batch, dry_run)
                batch = []
        
        except Exception as e:
            logger.error(f"Error processing paper {paper.get('link')}: {e}")
            continue
    
    # Upload remaining batch
    if batch:
        total += upsert_batch(supabase, 'papers_dct', batch, dry_run)
    
    logger.info(f"Completed paper push: {total} papers")
    return total


def push_metrics(
    supabase: Client,
    metrics_json_path: str,
    valid_datasets: Set[str],
    valid_papers: Set[str],
    batch_size: int = 500,
    dry_run: bool = False,
    paper_dataset_matches_path: Optional[str] = None,
    paper_aliases: Optional[Dict[str, str]] = None,
) -> int:
    """
    Push metrics from JSON to Supabase.
    
    Supports two formats:
    1. New format: List of records with paper_id, task_type, and metrics
    2. Legacy format: Nested dict with dataset_slug -> task -> paper_url -> metrics
    
    For the new format, paper-dataset matches are used to associate metrics
    with datasets. Only metrics that can be linked to a valid dataset will
    be pushed.
    
    Args:
        supabase: Supabase client
        metrics_json_path: Path to metrics JSON file
        valid_datasets: Set of valid dataset slugs (for FK validation)
        valid_papers: Set of valid paper URLs (for FK validation)
        batch_size: Number of rows per batch
        dry_run: If True, only log what would be inserted
        paper_dataset_matches_path: Optional path to paper-dataset matches JSONL file
    
    Returns:
        Total number of metrics processed
    """
    logger.info(f"Loading metrics from {metrics_json_path}")
    
    with open(metrics_json_path, 'r', encoding="utf-8") as f:
        data = json.load(f)
    
    total = 0
    batch = []
    skipped = 0
    
    # Detect format: list (new) or dict (legacy)
    if isinstance(data, list):
        # New format: list of paper-level metrics with no dataset_slug
        # We need paper-dataset matches to associate metrics with datasets
        
        # Build paper_id -> dataset_slug mapping from matches
        paper_to_datasets: Dict[str, List[str]] = {}
        if paper_dataset_matches_path and Path(paper_dataset_matches_path).exists():
            logger.info(f"Loading paper-dataset matches from {paper_dataset_matches_path}")
            with open(paper_dataset_matches_path, 'r', encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        match = json.loads(line)
                        if match.get('is_match', False):
                            paper_id = match.get('paper_id', '')
                            dataset_name = match.get('dataset_name', '')
                            if paper_id and dataset_name:
                                if paper_id not in paper_to_datasets:
                                    paper_to_datasets[paper_id] = []
                                paper_to_datasets[paper_id].append(dataset_name)
                    except json.JSONDecodeError:
                        continue
            logger.info(f"Loaded {len(paper_to_datasets)} paper-dataset associations")
        else:
            logger.warning(
                "No paper-dataset matches file found. "
                "Metrics cannot be associated with datasets."
            )
        
        # Map 2.1's dataset names onto the slugs final_datasets was pushed with.
        def _to_slug(name: str) -> Optional[str]:
            forms = _slug_forms(name)
            if not valid_datasets:
                return sorted(forms, key=len)[0] if forms else None
            found = forms & valid_datasets
            return next(iter(found)) if found else None
        paper_to_slugs = {pid: sorted({s for s in map(_to_slug, names) if s})
                          for pid, names in paper_to_datasets.items()}

        logger.info(f"Processing {len(data)} metric records from new format")
        unresolved = 0
        # Some papers report several results under one context (paper, dataset, task,
        # split, level, subset) -- e.g. one per configuration on the same test set: ~10%
        # of rows in the 2026-10-27/28 smoke runs, none identical. One key per context made
        # the upsert fail ("cannot affect row a second time") or keep one of them, so
        # repeats get a numbered key. Keys follow the file's order, so re-pushing the same
        # file updates the same rows.
        seen_keys: Dict[str, int] = {}
        repeated = 0
        for record in data:
            try:
                paper_id = record.get('paper_id', '')
                task_type = record.get('task_type', '')
                metrics = record.get('metrics', {})

                if task_type not in ('classification', 'segmentation', 'detection', 'regression', 'survival'):
                    logger.warning(f"Invalid task type for paper {paper_id}: {task_type}")
                    skipped += 1
                    continue
                # A record merged into another of the same paper: its metrics go to the kept row.
                linked_id = (paper_aliases or {}).get(paper_id, paper_id)
                paper_url = canonicalize_url(linked_id) or linked_id
                if valid_papers and paper_url not in valid_papers:
                    logger.debug(f"Paper {paper_id} not in valid_papers, skipping")
                    skipped += 1
                    continue

                # Each result belongs to ONE dataset: the linked catalog dataset it
                # names, or none (paper-level row: private cohort, or ambiguous).
                dataset_slug = resolve_metric_dataset(record.get('dataset', ''), paper_to_slugs.get(paper_id, []))
                unresolved += dataset_slug is None
                row = build_metric_row(paper_url, dataset_slug, task_type, metrics, record)
                n = seen_keys.get(row['metric_key'], 0)
                seen_keys[row['metric_key']] = n + 1
                if n:
                    repeated += 1
                    row['metric_key'] = hashlib.md5(f"{row['metric_key']}|{n + 1}".encode()).hexdigest()
                batch.append(row)

                if len(batch) >= batch_size:
                    total += upsert_batch(supabase, 'filtered_metrics', batch, dry_run)
                    batch = []

            except Exception as e:
                logger.error(f"Error processing metric record: {e}")
                skipped += 1
                continue
        logger.info(f"{unresolved} metric rows are paper-level (dataset outside the catalog or ambiguous)")
        logger.info(f"{repeated} metric rows repeat another row's context and got a numbered key")

    elif isinstance(data, dict):
        # Legacy format: {dataset_slug: {task: {paper_url: metrics}}}
        logger.info("Detected legacy metrics format (nested dict)")
        
        for dataset_slug, tasks in data.items():
            for task, papers in tasks.items():
                for paper_url, metrics in papers.items():
                    try:
                        # Validate task type
                        if task not in ('classification', 'segmentation', 'detection', 'regression', 'survival'):
                            logger.warning(f"Invalid task type: {task}")
                            skipped += 1
                            continue
                        
                        # Validate foreign keys
                        if valid_datasets and dataset_slug not in valid_datasets:
                            logger.warning(f"Invalid dataset_slug: {dataset_slug}")
                            skipped += 1
                            continue
                        
                        paper_url_canon = canonicalize_url(paper_url)
                        if valid_papers and paper_url_canon not in valid_papers:
                            logger.warning(f"Invalid paper_url: {paper_url}")
                            skipped += 1
                            continue
                        
                        row = build_metric_row(paper_url_canon or paper_url, dataset_slug, task, metrics)
                        batch.append(row)
                        
                        if len(batch) >= batch_size:
                            total += upsert_batch(supabase, 'filtered_metrics', batch, dry_run)
                            batch = []
                    
                    except Exception as e:
                        logger.error(f"Error processing metric for {paper_url}/{dataset_slug}/{task}: {e}")
                        skipped += 1
                        continue
    
    else:
        logger.error(f"Unknown metrics format: expected list or dict, got {type(data)}")
        return 0
    
    # Upload remaining batch
    if batch:
        total += upsert_batch(supabase, 'filtered_metrics', batch, dry_run)
    
    logger.info(f"Completed metrics push: {total} metrics ({skipped} skipped)")
    return total


def push_to_supabase(
    supabase_url: str,
    supabase_key: str,
    datasets_json: Optional[str] = None,
    papers_json: Optional[str] = None,
    metrics_json: Optional[str] = None,
    batch_size: int = 500,
    dry_run: bool = False,
    init_db_url: Optional[str] = None,
    paper_dataset_matches_path: Optional[str] = None
) -> Dict[str, int]:
    """
    Main function to push all data to Supabase.
    
    Args:
        supabase_url: Supabase project URL
        supabase_key: Supabase service role key
        datasets_json: Path to datasets JSON file
        papers_json: Path to papers JSON file
        metrics_json: Optional path to metrics JSON file
        batch_size: Number of rows per batch
        dry_run: If True, only log what would be inserted
        init_db_url: Optional database URL for schema initialization
        paper_dataset_matches_path: Optional path to paper-dataset matches JSONL file
    
    Returns:
        Dictionary with counts of processed items
    """
    # Optionally initialize DB schema before connecting via REST
    if init_db_url:
        try:
            init_database(init_db_url)
        except Exception as e:
            logger.warning(f"Database initialization skipped: {e}")

    FAILED_ROWS.clear()

    # Initialize Supabase client
    supabase: Client = create_client(supabase_url, supabase_key)
    logger.info(f"Connected to Supabase at {supabase_url}")
    
    results = {
        'datasets': 0,
        'papers': 0,
        'metrics': 0
    }
    
    # Track valid IDs for FK validation
    valid_datasets = set()
    valid_papers = set()
    
    # Step 1: Push datasets
    if datasets_json:
        logger.info("\n=== Pushing Datasets ===")
        results['datasets'] = push_datasets(supabase, datasets_json, batch_size, dry_run)
        
        # Collect valid dataset slugs
        with open(datasets_json, 'r', encoding="utf-8") as f:
            data = json.load(f)
            for ds in data.get('datasets', []):
                slug = normalize_value(ds.get('slug'))
                if slug:
                    valid_datasets.add(slug)
    
    # Step 2: Push papers
    if papers_json:
        logger.info("\n=== Pushing Papers ===")
        results['papers'] = push_papers(supabase, papers_json, batch_size, dry_run)
        
        # Collect valid paper URLs, and where merged-away records' metrics now belong
        with open(papers_json, 'r', encoding="utf-8") as f:
            index = PaperIndex(json.load(f))
        for paper in index.records:
            url = canonicalize_url(paper.get('link'))
            if url:
                valid_papers.add(url)
        paper_aliases = index.aliases()
    
    # Step 3: Push metrics (if provided)
    if metrics_json and not papers_json:
        paper_aliases = {}
    if metrics_json:
        logger.info("\n=== Pushing Metrics ===")
        results['metrics'] = push_metrics(
            supabase,
            metrics_json,
            valid_datasets,
            valid_papers,
            batch_size,
            dry_run,
            paper_dataset_matches_path,
            paper_aliases,
        )
    
    logger.info("\n=== Push Complete ===")
    logger.info(f"Datasets: {results['datasets']}")
    logger.info(f"Papers: {results['papers']}")
    logger.info(f"Metrics: {results['metrics']}")
    results['failed'] = {table: len(rows) for table, rows in FAILED_ROWS.items()}
    for table, rows in FAILED_ROWS.items():
        logger.error(f"{len(rows)} rows refused by {table}, e.g. {rows[0]['key']}: {rows[0]['error'][:200]}")

    return results
