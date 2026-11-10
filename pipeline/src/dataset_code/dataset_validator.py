"""Module for validating and extracting dataset metadata."""
import json
import os
import logging
import re
import time
import ast
from pathlib import Path
from typing import Dict, Optional, List
from urllib.parse import urlparse
from jsonschema import validate, ValidationError
from pipeline.src.paper_code.standardize import normalize_staining_type
from pipeline.utils.llms.llm import run_gpt, strip_json_wrapper, batch_extract_with_validation

logger = logging.getLogger(__name__)

# Exceptions that indicate a defect in this code rather than a problem with the data or
# the upstream service. These must never be absorbed into a business outcome such as
# "dataset rejected" -- see the handler in validate_pathology_dataset.
BUG_EXCEPTIONS = (NameError, AttributeError, TypeError, ImportError, IndentationError)


def validate_and_normalize_metadata_for_batch(metadata: Dict, schema: Dict) -> tuple[bool, Optional[str]]:
    normalized = _normalize_metadata_fields(metadata or {}, schema)
    metadata.clear()
    metadata.update(normalized)
    return True, None


def merge_search_results(
    serper_results: List[Dict],
    llm_results: Dict,
    summaries: List[Dict]
) -> Dict:
    """
    Merge search results from multiple sources.
    
    Args:
        serper_results: List of Serper search results
        llm_results: Dict with results from Perplexity, GPT, Gemini
        summaries: List of page summaries
        
    Returns:
        Merged dict with all search information
    """
    merged = {
        "serper_results": serper_results,
        "llm_results": llm_results,
        "page_summaries": summaries,
        "sources": []
    }
    
    # Collect all text sources
    sources = []
    
    # Add Serper snippets
    for result in serper_results:
        if result.get('snippet'):
            sources.append({
                "source": "serper",
                "url": result.get('link', ''),
                "text": result.get('snippet', '')
            })
    
    # Add LLM results
    for provider, result in llm_results.items():
        if result.get('content'):
            sources.append({
                "source": provider,
                "text": result.get('content', '')
            })
    
    # Add page summaries
    for summary in summaries:
        if summary.get('summary'):
            sources.append({
                "source": "page_summary",
                "url": summary.get('page_path', ''),
                "text": summary.get('summary', '')
            })
    
    merged["sources"] = sources
    merged["combined_text"] = "\n\n".join(s["text"] for s in sources if s.get("text"))
    
    return merged


# Dataset `task` spellings that mean one schema value.
TASK_SYNONYMS = {"semantic segmentation": "segmentation", "instance segmentation": "segmentation",
                 "nuclei segmentation": "segmentation"}


def prevalidate_datasets(
    descriptions_by_name: Dict[str, List[str]],
    prompt_path: Path,
    cache_path: Path,
    use_gemini: bool = False,
    workers: int = 32,
) -> set:
    """Judge "is this a pathology dataset" from paper descriptions alone (step 2.0).

    Runs before 2.1 so matching and the 2.2 web search skip non-pathology
    datasets. Uses the 2.2c prompt with the paper-written descriptions in place
    of search results -- the exact input measured against full web-search
    validation on the January catalog (99% of its accepts kept).

    Only datasets that have descriptions are judged. Without one the name alone
    loses 17% of real datasets, so those are kept and left to 2.2c. A failed call
    also keeps the dataset: this step may only remove what it positively rejects.
    Verdicts are cached by name.

    Returns the names to drop.
    """
    from concurrent.futures import ThreadPoolExecutor

    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    prompt = Path(prompt_path).read_text(encoding="utf-8")
    model = (os.getenv("WSI_EXTRACTION_MODEL", "gemini-2.5-pro") if use_gemini else os.getenv("WSI_DATASET_MODEL", "gpt-6-luna"))

    def judge(name: str):
        context = "\n".join(f"- {d}" for d in descriptions_by_name[name][:3])
        user_prompt = (f"Dataset Name: {name}\n\nSearch Results:\n(none available)\n\n"
                       f"How research papers that use this dataset describe it:\n{context}")
        try:
            response = run_gpt(system_prompt=prompt, user_prompt=user_prompt, model=model,
                               response_format={"type": "json_object"}, use_gemini=use_gemini)
            result = json.loads(strip_json_wrapper(response.choices[0].message.content or "{}") or "{}")
            verdict = result.get("is_pathology_dataset")
            return verdict if isinstance(verdict, bool) else None
        except BUG_EXCEPTIONS:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Pre-validation failed for {name!r}; keeping it: {e}")
            return None

    # A failed call is cached as None (kept); it is asked again on the next run.
    todo = [n for n, d in descriptions_by_name.items() if d and cache.get(n) is None]
    if todo:
        logger.info(f"Pre-validating {len(todo)} datasets from paper descriptions")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for name, verdict in zip(todo, ex.map(judge, todo)):
                cache[name] = verdict
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    return {n for n in descriptions_by_name if cache.get(n) is False}


def validate_pathology_dataset(
    merged_results: Dict,
    entity_name: str,
    prompt_path: Path,
    use_gemini: bool = False,
    entity_type: str = "dataset"
) -> Dict:
    """
    Validate that a dataset or model is pathology/WSI related.
    
    Args:
        merged_results: Merged search results
        entity_name: Name of dataset or model
        prompt_path: Path to validate_pathology_dataset.md or validate_pathology_model.md
        use_gemini: Whether to use Gemini
        entity_type: Type of entity ('dataset' or 'model')
        
    Returns:
        Validation result dict
    """
    # Read prompt
    with open(prompt_path, 'r', encoding="utf-8") as f:
        prompt = f.read()
    
    # Build user prompt with merged results
    combined_text = merged_results.get('combined_text', '')
    
    # Limit text length
    max_chars = 10000
    if len(combined_text) > max_chars:
        combined_text = combined_text[:max_chars] + "\n\n[Content truncated...]"
    
    entity_label = "Dataset Name" if entity_type == "dataset" else "Model Name"
    user_prompt = f"{entity_label}: {entity_name}\n\nSearch Results:\n{combined_text}"
    
    validation_key = 'is_pathology_dataset' if entity_type == 'dataset' else 'is_pathology_model'
    last_error = None
    for attempt in range(3):
        try:
            response = run_gpt(
                system_prompt=prompt,
                user_prompt=user_prompt,
                model=os.getenv("WSI_EXTRACTION_MODEL", "gemini-2.5-pro") if use_gemini else os.getenv("WSI_DATASET_MODEL", "gpt-6-luna"),
                response_format={"type": "json_object"},
                use_gemini=use_gemini
            )

            result_text = response.choices[0].message.content or "{}"
            # gemini-3.1-pro-preview wraps JSON in markdown fences despite json mode
            result = json.loads(strip_json_wrapper(result_text) or "{}")
            if isinstance(result.get(validation_key), bool):
                logger.info(f"Validation for {entity_name}: {result[validation_key]}")
                return result
            last_error = f"no {validation_key} verdict in response"

        except BUG_EXCEPTIONS:
            # A NameError/AttributeError/TypeError is a bug in this file, not a property of
            # the dataset. Swallowing it into `is_pathology_dataset: False` makes every
            # dataset look legitimately rejected -- exactly what happened on 2026-08-02,
            # when a missing `import os` silently rejected 3/3 datasets with
            # "Error during validation: name 'os' is not defined" buried in a rationale
            # field nobody reads. Fail loudly instead.
            logger.exception("BUG while validating %s %s -- re-raising", entity_type, entity_name)
            raise
        except Exception as e:
            last_error = str(e)
            logger.warning(f"Validation call failed for {entity_type} {entity_name} (attempt {attempt + 1}/3): {e}")
        time.sleep(2.0 * (attempt + 1))

    # No verdict. This is a failed call, not a judgment: callers must neither reject the
    # dataset nor cache this. In January 2026, 328 connection errors were returned as
    # `is_pathology_dataset: False`, cached, and silently dropped 217 catalog datasets
    # (BreaKHis, CAMELYON16/17, CoNIC, ...) from every later run.
    logger.error(f"Validation failed for {entity_type} {entity_name} after 3 attempts: {last_error}")
    return {"error": last_error or "validation_failed", "disqualifier": "validation_error"}


def validation_call_failed(validation: Optional[Dict]) -> bool:
    """True when a validation result is a failed call rather than a verdict (including
    the pre-2026-10 cached form, which recorded the failure as a rejection)."""
    return (not validation or bool(validation.get("error"))
            or validation.get("disqualifier") == "validation_error")


def extract_dataset_metadata(
    merged_results: Dict,
    entity_name: str,
    prompt_path: Path,
    schema_path: Path,
    use_gemini: bool = False,
    entity_type: str = "dataset"
) -> Dict:
    """
    Extract dataset or model metadata using LLM with validation and retry.
    
    Args:
        merged_results: Merged search results
        entity_name: Name of dataset or model
        prompt_path: Path to ex_data.md or ex_model.md
        schema_path: Path to dataset_fields_schema.json
        use_gemini: Whether to use Gemini
        entity_type: Type of entity ('dataset' or 'model')
        
    Returns:
        Extracted metadata dict
    """
    import time
    import random
    from pipeline.utils.llms.llm import validate_json_response, is_all_empty_values
    
    # Read schema
    with open(schema_path, 'r', encoding="utf-8") as f:
        schema = json.load(f)
    
    # Read prompt
    with open(prompt_path, 'r', encoding="utf-8") as f:
        prompt_template = f.read()
    
    # Format prompt with schema fields
    fields_description = json.dumps(schema.get('schema', {}).get('properties', {}), indent=2)
    prompt = prompt_template.replace('{fields}', fields_description)
    
    # Build user prompt
    combined_text = merged_results.get('combined_text', '')
    max_chars = 10000
    if len(combined_text) > max_chars:
        combined_text = combined_text[:max_chars] + "\n\n[Content truncated...]"
    
    entity_label = "Dataset Name" if entity_type == "dataset" else "Model Name"
    user_prompt = f"{entity_label}: {entity_name}\n\nSummaries:\n{combined_text}"
    
    # Retry logic for invalid/empty responses
    max_attempts = 3
    last_error = None
    
    for attempt in range(max_attempts):
        try:
            response = run_gpt(
                system_prompt=prompt,
                user_prompt=user_prompt,
                model=os.getenv("WSI_EXTRACTION_MODEL", "gemini-2.5-pro") if use_gemini else os.getenv("WSI_DATASET_MODEL", "gpt-6-luna"),
                response_format={"type": "json_object"},
                use_gemini=use_gemini,
                validate_json=True,
                reject_all_empty=True,
                max_retries=1
            )
            
            result_text = response.choices[0].message.content or "{}"
            
            # Validate the response
            is_valid, parsed, error = validate_json_response(
                result_text,
                reject_all_empty=True
            )
            
            if is_valid and parsed:
                logger.info(f"Extracted metadata for {entity_name}")
                return parsed
            
            last_error = error
            logger.warning(f"Invalid metadata response for {entity_name} (attempt {attempt + 1}): {error}")
            
        except Exception as e:
            last_error = str(e)
            logger.warning(f"Error extracting metadata for {entity_name} (attempt {attempt + 1}): {e}")
        
        if attempt < max_attempts - 1:
            time.sleep(min(2.0 * (2 ** attempt), 10.0) + random.random() * 0.25)
    
    logger.error(f"Failed to extract metadata for {entity_name} after {max_attempts} attempts: {last_error}")
    return {"error": last_error or "extraction_failed"}


def batch_extract_dataset_metadata(
    items: List[tuple],
    prompt_path: Path,
    schema_path: Path,
    use_gemini: bool = False,
    entity_type: str = "dataset"
) -> List[Optional[Dict]]:
    """
    Batch extract dataset or model metadata using LLM with validation and retries.
    
    Args:
        items: List of (entity_name, merged_results) tuples
        prompt_path: Path to ex_data.md or ex_model.md
        schema_path: Path to dataset_fields_schema.json
        use_gemini: Whether to use Gemini
        entity_type: Type of entity ('dataset' or 'model')
        
    Returns:
        List of extracted and validated metadata dicts (None on failure)
    """
    # Read schema
    with open(schema_path, 'r', encoding="utf-8") as f:
        schema_def = json.load(f)
    
    schema = schema_def.get('schema', {})
    
    # Read prompt
    with open(prompt_path, 'r', encoding="utf-8") as f:
        prompt_template = f.read()
    
    # Format prompt with schema fields
    fields_description = json.dumps(schema.get('properties', {}), indent=2)
    prompt = prompt_template.replace('{fields}', fields_description)
    
    # Build batch items with formatted user prompts
    batch_items = []
    for entity_name, merged_results in items:
        combined_text = merged_results.get('combined_text', '')
        max_chars = 10000
        if len(combined_text) > max_chars:
            combined_text = combined_text[:max_chars] + "\n\n[Content truncated...]"
        
        entity_label = "Dataset Name" if entity_type == "dataset" else "Model Name"
        user_prompt = f"{entity_label}: {entity_name}\n\nSummaries:\n{combined_text}"
        batch_items.append((entity_name, user_prompt))
    
    # Create validator function that normalizes and validates
    def validator(metadata: Dict) -> tuple[bool, Optional[str]]:
        """Validate and normalize metadata against schema."""
        try:
            return validate_and_normalize_metadata_for_batch(metadata, schema)
        except Exception as e:
            return False, str(e)
    
    # Run batch extraction with validation
    results = batch_extract_with_validation(
        items=batch_items,
        prompt=prompt,
        step_name=f"extract_{entity_type}_metadata",
        validator=validator,
        use_gemini=use_gemini,
        max_retries=2
    )
    
    # Normalize all successful results
    normalized_results = []
    for result in results:
        if result is not None:
            normalized = normalize_metadata(result, schema_path)
            normalized_results.append(normalized)
        else:
            normalized_results.append(None)
    
    return normalized_results


def _normalize_metadata_fields(metadata: Dict, schema: Dict) -> Dict:
    """Helper function to normalize metadata fields without validation.
    
    This is used by the validator to normalize before validation.
    """
    # Helper to check if value is empty/null/nan/unknown
    def _is_empty(val) -> bool:
        if val is None:
            return True
        if isinstance(val, str):
            return val.strip().lower() in {'nan', 'null', 'none', 'n/a', 'unknown', ''}
        return False

    def _clean_scalar_string(val: str) -> str:
        s = str(val).strip()
        if not s:
            return ''

        # Remove stray leading quotes/backticks that sometimes appear
        while s and s[0] in ('"', "'", '`') and not (len(s) >= 2 and s[0] == s[-1]):
            s = s[1:].lstrip()
        # Remove trailing commas or quote+comma artifacts
        s = re.sub(r"[\s,]+$", "", s).strip()
        lowered = s.lower()
        if lowered in {'nan', 'null', 'none', 'n/a', 'unknown'}:
            return ''
        if lowered == '0':
            return ''
        return s

    def _coerce_pipe_field(val: object) -> str:
        if _is_empty(val):
            return ''
        if isinstance(val, list):
            parts: list[str] = []
            for x in val:
                if isinstance(x, (str, int, float)):
                    s = _clean_scalar_string(str(x))
                    if s:
                        parts.append(s)
            return "|".join(parts)
        if isinstance(val, (str, int, float)):
            return _clean_scalar_string(str(val))
        return ''

    def _allowed_tokens_from_pipe_pattern(pattern: str) -> Optional[set[str]]:
        if not isinstance(pattern, str) or not pattern:
            return None
        m = re.match(r"^\^\(([^)]+)\)", pattern)
        if not m:
            return None
        raw = m.group(1)
        toks = [t.strip() for t in raw.split('|') if t.strip()]
        if not toks:
            return None
        return set(toks)

    def _sanitize_pipe_enum_value(raw: str, allowed: set[str]) -> str:
        if _is_empty(raw):
            return ''
        parts = [p.strip().lower() for p in str(raw).split('|') if p.strip()]
        if not parts:
            return ''

        can_other = 'other' in allowed
        out: list[str] = []
        seen: set[str] = set()
        for p in parts:
            if p in allowed:
                token = p
            elif can_other:
                token = 'other'
            else:
                continue
            if token not in seen:
                out.append(token)
                seen.add(token)
        return '|'.join(out)

    def _maybe_parse_dict_string(val: object) -> object:
        if not isinstance(val, str):
            return val
        s = val.strip()
        if not s:
            return val
        # If it's a JSON string containing an object (escaped quotes), parse it.
        # Examples: "{\"a\": 1}" or "{'a': 1}"
        candidates = [s]
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
            candidates.append(s[1:-1].strip())
        for cand in candidates:
            if cand.startswith('{') and cand.endswith('}'):
                for variant in (cand, cand.replace('\\"', '"').replace("\\'", "'")):
                    try:
                        return json.loads(variant)
                    except Exception:
                        pass
                try:
                    parsed = ast.literal_eval(cand)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    pass
        return val

    try:
        allowed_keys = set((schema.get('properties') or {}).keys())
        if allowed_keys:
            metadata = {k: v for k, v in metadata.items() if k in allowed_keys}
    except Exception:
        pass

    try:
        props = (schema.get('properties') or {})
        for field in (schema.get('required') or []):
            if field not in metadata or _is_empty(metadata.get(field)):
                rules = props.get(field) or {}
                t = rules.get('type')
                if field == 'paper':
                    metadata[field] = {"doi": "", "venue": ""}
                elif field == 'is_public':
                    metadata[field] = False
                elif field == 'labels':
                    metadata[field] = 'na'
                elif t == 'boolean':
                    metadata[field] = False
                elif t == 'integer':
                    metadata[field] = 0
                elif t == 'number':
                    metadata[field] = 0.0
                elif t == 'object':
                    metadata[field] = {}
                else:
                    metadata[field] = ''
    except Exception:
        pass
    
    # Generate slug if not present (support dataset or model)
    if 'slug' not in metadata or _is_empty(metadata.get('slug')):
        base_name = metadata.get('dataset_name') or metadata.get('model_name')
        if isinstance(base_name, str) and base_name.strip():
            slug = base_name.lower()
            slug = re.sub(r'[^a-z0-9]+', '-', slug)
            slug = slug.strip('-')
            metadata['slug'] = slug
        else:
            metadata['slug'] = 'unknown'
    
    # Ensure is_public exists (LLM may omit); default to False for safety
    if 'is_public' not in metadata or _is_empty(metadata.get('is_public')):
        metadata['is_public'] = False

    # Normalize boolean fields
    if 'is_public' in metadata:
        if isinstance(metadata['is_public'], str):
            metadata['is_public'] = metadata['is_public'].lower() in ['true', 'yes', '1']
        elif metadata['is_public'] is None:
            metadata['is_public'] = False
    
    # Normalize numeric fields (integers and floats)
    integer_fields = ['year', 'mag_min', 'mag_max', 'tile_size_px', 
                     'n_patients', 'n_slides', 'n_tiles', 'n_centers']
    float_fields = ['mpp', 'approx_size_gb']
    
    # Unstated numbers are 0 ("not stated", the prompt's convention and the schema's).
    # This used to write tile_size_px=32 and mpp=0.5 for unstated, unparsable or
    # out-of-range values -- the fourth path that made a default look like an extraction
    # (607 of 715 catalog entries carried mpp 0.5, 445 tile 32).
    for field in integer_fields:
        if field in metadata:
            val = metadata[field]
            try:
                coerced = 0 if _is_empty(val) or isinstance(val, bool) else int(float(val))
            except (ValueError, TypeError):
                coerced = 0
            metadata[field] = max(coerced, 0) if field != 'year' else coerced

    for field in float_fields:
        if field in metadata:
            val = metadata[field]
            try:
                coerced = 0.0 if _is_empty(val) or isinstance(val, bool) else float(val)
            except (ValueError, TypeError):
                coerced = 0.0
            if coerced < 0 or (field == 'mpp' and coerced > 10):
                coerced = 0.0
            metadata[field] = coerced

    # Note: n_patients, n_slides, etc. are now handled as integers above
    # No special "nan" string handling needed - they get converted to 0
    
    # Unstated stays '' -- 'other' asserts a non-standard stain was used.
    if 'staining_type' in metadata:
        metadata['staining_type'] = normalize_staining_type(_coerce_pipe_field(metadata['staining_type']))

    # Normalize simple enum fields. Where the source is silent, leave the value empty
    # rather than substituting a plausible one -- a fabricated value is indistinguishable
    # from a real extraction downstream. 'unknown' is retained for the two fields whose
    # enums define it as a legitimate member.
    #
    # This used to default domain->'histopathology', wsi_or_patch->'both',
    # tiling_strategy/split_method->'other' and split_level->'patient'. The split_level
    # default in particular asserted a methodological claim the source never made.
    simple_enum_defaults = {
        'domain': '',
        'wsi_or_patch': '',
        'tiling_strategy': '',
        'split_level': '',
        'split_method': '',
        'dataset_access': 'unknown',
        'license': 'unknown'
    }

    for field, default in simple_enum_defaults.items():
        if field in metadata:
            raw = _coerce_pipe_field(metadata[field])
            if _is_empty(raw):
                metadata[field] = default
            else:
                metadata[field] = raw.strip().lower() or default

    # Normalize license casing to match schema enums
    if 'license' in metadata and isinstance(metadata['license'], str):
        raw_license = metadata['license'].strip()
        normalized_key = raw_license.lower().replace(' ', '-').replace('_', '-')
        license_map = {
            'cc0': 'CC0',
            'cc-by': 'CC-BY',
            'cc-by-nc': 'CC-BY-NC',
        }
        if normalized_key in license_map:
            metadata['license'] = license_map[normalized_key]
    
    # Normalize pipe-delimited enum fields. An empty value stays empty: 'other' is a
    # positive claim ("a category outside this enum was used"), which is not the same
    # as "the source never said". Defaulting here was the last of three paths that
    # made unstated values indistinguishable from extracted ones -- and it ran after
    # the staining_type handling above, silently overwriting it. (staining_type is
    # normalized above against the shared list and is not re-sanitized here.)
    pipe_enum_fields = [
        'modality', 'tissue_site', 'species', 'sample_prep',
        'image_format', 'task', 'annotation_level',
        'annotation_format', 'annotation_types',
        # Model-specific fields
        'model_type', 'biological_domain', 'input_format', 'framework'
    ]

    for field in pipe_enum_fields:
        if field in metadata:
            raw = _coerce_pipe_field(metadata[field])
            if _is_empty(raw):
                metadata[field] = ''
            else:
                # Lowercase and collapse whitespace around pipes
                raw = raw.replace(' | ', '|').replace('| ', '|').replace(' |', '|')
                raw = raw.strip().lower()
                if field == 'task':
                    # The schema still admits "semantic segmentation", so the prompt's
                    # "map synonyms" rule alone left both spellings in the catalog.
                    tokens = [TASK_SYNONYMS.get(t.strip(), t.strip()) for t in raw.split('|')]
                    raw = '|'.join(dict.fromkeys(t for t in tokens if t))
                rules = (schema.get('properties') or {}).get(field) or {}
                allowed = None
                try:
                    allowed = _allowed_tokens_from_pipe_pattern(rules.get('pattern'))
                except Exception:
                    allowed = None
                if allowed:
                    sanitized = _sanitize_pipe_enum_value(raw, allowed)
                    if _is_empty(sanitized):
                        metadata[field] = 'other' if 'other' in allowed else (raw or '')
                    else:
                        metadata[field] = sanitized
                    # The prompts' rule, enforced: four or more distinct organs is multi-organ.
                    if field == 'tissue_site' and 'multi-organ' in allowed:
                        organs = {t for t in metadata[field].split('|') if t not in ('', 'other', 'multi-organ')}
                        if len(organs) >= 4:
                            metadata[field] = 'multi-organ'
                else:
                    metadata[field] = raw or 'other'
    
    # Handle simple enum field for models (license_type)
    if 'license_type' in metadata:
        raw = _coerce_pipe_field(metadata['license_type'])
        if _is_empty(raw):
            metadata['license_type'] = 'other'
        else:
            metadata['license_type'] = raw.strip().lower() or 'other'
    
    # Handle boolean fields
    boolean_fields = ['external_validation', 'is_public', 'pretrained']
    for field in boolean_fields:
        if field in metadata:
            if _is_empty(metadata[field]):
                metadata[field] = False
            elif isinstance(metadata[field], str):
                val = metadata[field].strip().lower()
                metadata[field] = val in ['true', 'yes', '1', 't', 'y']
            elif not isinstance(metadata[field], bool):
                metadata[field] = False
    
    # Handle object fields (like tissue_site_code)
    if 'tissue_site_code' in metadata:
        val = _maybe_parse_dict_string(metadata['tissue_site_code'])
        if _is_empty(val) or not isinstance(val, dict):
            # Provide empty but valid object structure
            metadata['tissue_site_code'] = {
                "system": "UBERON",
                "code": ""
            }
        else:
            # Ensure required fields exist
            if 'system' not in val or _is_empty(val.get('system')):
                val['system'] = 'UBERON'
            if 'code' not in val or _is_empty(val.get('code')):
                val['code'] = ''
            metadata['tissue_site_code'] = val
    
    # Handle paper object field
    if 'paper' in metadata:
        val = _maybe_parse_dict_string(metadata['paper'])
        if _is_empty(val) or not isinstance(val, dict):
            # Provide empty but valid object structure
            metadata['paper'] = {
                "doi": "",
                "venue": ""
            }
        else:
            allowed_paper_fields = {'doi', 'venue'}
            val = {k: v for k, v in val.items() if k in allowed_paper_fields}
            for subfield in ['doi', 'venue']:
                if subfield not in val or _is_empty(val.get(subfield)):
                    val[subfield] = ''
                elif not isinstance(val[subfield], str):
                    val[subfield] = str(val[subfield])
                else:
                    val[subfield] = _clean_scalar_string(val[subfield])
            metadata['paper'] = val
    
    # Ensure all string fields are actually strings (not None or other types)
    string_fields = ['slug', 'dataset_name', 'model_name', 'description', 'code_url', 
                    'download_url', 'home_page_url', 'download_link', 'scanner', 'labels', 
                    'institutions', 'resource_urls', 'architecture', 'input_size', 
                    'magnification', 'pretraining_data', 'num_parameters', 
                    'paper_reference', 'code_repository']
    for field in string_fields:
        if field in metadata:
            if _is_empty(metadata[field]):
                metadata[field] = ''
            elif not isinstance(metadata[field], str):
                metadata[field] = str(metadata[field])
            else:
                metadata[field] = _clean_scalar_string(metadata[field])
    
    # Enforce string maxLength constraints from schema
    try:
        properties = schema.get('properties', {})
        # Truncate long string fields
        for field, rules in properties.items():
            if field in metadata and isinstance(metadata[field], str):
                max_len = rules.get('maxLength')
                if isinstance(max_len, int) and max_len > 0:
                    if len(metadata[field]) > max_len:
                        metadata[field] = metadata[field][:max_len]
    except Exception as e:
        logger.warning(f"String normalization skipped due to error: {e}")
    
    return metadata


def normalize_metadata(metadata: Dict, schema_path: Path) -> Dict:
    """
    Normalize and validate metadata against schema.
    
    Args:
        metadata: Raw extracted metadata
        schema_path: Path to dataset_fields_schema.json
        
    Returns:
        Normalized and validated metadata
    """
    # Read schema
    with open(schema_path, 'r', encoding="utf-8") as f:
        schema_def = json.load(f)
    
    schema = schema_def.get('schema', {})
    
    # Normalize fields using helper
    metadata = _normalize_metadata_fields(metadata, schema)
    
    # Validate against schema
    try:
        validate(instance=metadata, schema=schema)
        metadata['validation_passed'] = True
        logger.info(f"Metadata validation passed for {metadata.get('dataset_name', 'unknown')}")
    except ValidationError as e:
        logger.warning(f"Metadata validation failed: {e.message}")
        metadata['validation_passed'] = False
        metadata['validation_error'] = e.message
    
    return metadata


def find_download_link(
    merged_results: Dict,
    entity_name: str,
    prompt_path: Path,
    use_gemini: bool = False,
    entity_type: str = "dataset"
) -> Dict:
    """
    Find URLs for a dataset or model (code_url, download_url, home_page_url).
    
    Args:
        merged_results: Merged search results
        entity_name: Name of dataset or model
        prompt_path: Path to download_link.md
        use_gemini: Whether to use Gemini
        entity_type: Type of entity ('dataset' or 'model')
        
    Returns:
        Dict with code_url, download_url, home_page_url and metadata
    """
    # Read prompt
    with open(prompt_path, 'r', encoding="utf-8") as f:
        prompt = f.read()
    
    # Build search results summary
    results_text = ""
    for result in merged_results.get('serper_results', [])[:10]:
        results_text += f"Title: {result.get('title', '')}\n"
        results_text += f"URL: {result.get('link', '')}\n"
        results_text += f"Snippet: {result.get('snippet', '')}\n\n"
    
    entity_label = "Dataset Name" if entity_type == "dataset" else "Model Name"
    described = "".join(f"- {d}\n" for d in merged_results.get('paper_descriptions') or [])
    user_prompt = (f"{entity_label}: {entity_name}\n\n"
                   + (f"How research papers that use it describe it (pick URLs for THIS dataset):\n{described}\n"
                      if described else "")
                   + f"Search Results:\n{results_text}")
    
    def _is_valid_url(u: str) -> bool:
        try:
            pu = urlparse(u)
            return pu.scheme in ("http", "https") and bool(pu.netloc)
        except Exception:
            return False

    last_err = None
    answered = False  # did any call return a response at all?
    for attempt in range(3):
        try:
            response = run_gpt(
                system_prompt=prompt,
                user_prompt=user_prompt,
                model="gemini-3.1-flash-lite" if use_gemini else os.getenv("WSI_DATASET_MODEL", "gpt-6-luna"),
                response_format={"type": "json_object"},
                use_gemini=use_gemini,
                validate_json=True,
                reject_all_empty=False,
                max_retries=1
            )
            content = (response.choices[0].message.content or "").strip()
            answered = True
            
            # Try to parse as JSON (new format)
            try:
                import json
                urls = json.loads(strip_json_wrapper(content) or "{}")
                code_url = urls.get("code_url", "")
                download_url = urls.get("download_url", "")
                home_page_url = urls.get("home_page_url", "")
                
                # Validate at least one URL is valid
                has_valid_url = any([
                    _is_valid_url(code_url),
                    _is_valid_url(download_url),
                    _is_valid_url(home_page_url)
                ])
                
                if has_valid_url:
                    entity_key = "dataset_name" if entity_type == "dataset" else "model_name"
                    return {
                        "code_url": code_url,
                        "download_url": download_url,
                        "home_page_url": home_page_url,
                        "status": "found",
                        entity_key: entity_name
                    }
                last_err = "no_valid_urls_in_json"
            except json.JSONDecodeError:
                # Fallback: treat as single URL (backward compatibility)
                if _is_valid_url(content):
                    entity_key = "dataset_name" if entity_type == "dataset" else "model_name"
                    # Assume it's a home page URL if we can't determine type
                    return {
                        "code_url": "",
                        "download_url": "",
                        "home_page_url": content,
                        "status": "found",
                        entity_key: entity_name
                    }
                last_err = f"invalid_response: {content[:100]}"
        except Exception as e:
            last_err = str(e)
        time.sleep(1.5 * (attempt + 1))
    
    logger.warning(f"URLs not found for {entity_name}: {last_err}")
    entity_key = "dataset_name" if entity_type == "dataset" else "model_name"
    return {
        "code_url": "",
        "download_url": "",
        "home_page_url": "",
        # "error" = the calls failed (retry next run); "not_found" = the model found no URL.
        "status": "not_found" if answered else "error",
        entity_key: entity_name,
        "error": last_err or "not_found"
    }
