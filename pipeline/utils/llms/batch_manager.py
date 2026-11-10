"""Batch processing manager for GPT and Gemini LLM APIs.

This module provides functionality to:
1. Create batch jobs for both GPT and Gemini APIs
2. Poll batch job status with resumable state persistence
3. Retrieve and process batch results
4. Handle errors and retries gracefully
"""

import json
import os
import time
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Literal
from dataclasses import dataclass, asdict
from datetime import datetime
import logging

from openai import OpenAI

try:
    from google import genai
except ModuleNotFoundError:
    genai = None

logger = logging.getLogger(__name__)


# --- Batch sizing limits ------------------------------------------------------
#
# A single oversized batch is what killed the 2026-04-26 full-scale run: step 1.2
# submitted 12,834 fulltext requests as one 363 MB JSONL, and the job sat at
# 0/12834 for 224 minutes before dying on repeated 503s. Nothing here split it --
# the only size handling was padding the upload to an 8 MB boundary.
#
# Splitting by request count AND by serialized byte size keeps each job small
# enough to make progress, and (because each chunk gets its own step name) gives
# per-chunk state, caching and resume for free. A chunk that fails is retried
# without re-paying for the chunks that already succeeded.
# Bytes are the real constraint: April's 10,363-request snippet_filter batch completed
# fine, while the 363 MB fulltext upload did not. So the count cap is deliberately
# loose and the byte cap does the work. Keeping the chunk count low also matters
# because each sub-batch costs its own batch latency (hours), and they are submitted
# together but still add up if a step fragments into dozens of pieces.
MAX_REQUESTS_PER_BATCH = int(os.getenv("WSI_BATCH_MAX_REQUESTS", "20000"))
MAX_BATCH_BYTES = int(os.getenv("WSI_BATCH_MAX_BYTES", str(64 * 1024 * 1024)))  # 64 MB


def split_requests(
    requests: List[Dict],
    max_requests: int = MAX_REQUESTS_PER_BATCH,
    max_bytes: int = MAX_BATCH_BYTES,
) -> List[List[Dict]]:
    """Split requests into chunks bounded by both count and serialized size.

    A single request larger than ``max_bytes`` still gets its own chunk rather than
    being dropped -- oversized-but-submitted beats silently missing.
    """
    if not requests:
        return []

    chunks: List[List[Dict]] = []
    current: List[Dict] = []
    current_bytes = 0

    for req in requests:
        size = len(json.dumps(req).encode("utf-8")) + 1  # +1 for the newline
        would_exceed = current and (
            len(current) >= max_requests or current_bytes + size > max_bytes
        )
        if would_exceed:
            chunks.append(current)
            current, current_bytes = [], 0
        current.append(req)
        current_bytes += size

    if current:
        chunks.append(current)
    return chunks


def _sum_usage(results: Dict[str, Dict]) -> Tuple[int, int]:
    """Total (input_tokens, output_tokens) across a step's batch results.

    Handles both provider shapes:
      GPT     result['response']['body']['usage'] -> prompt_tokens / completion_tokens
      Gemini  result['response']['usageMetadata'] -> promptTokenCount /
              candidatesTokenCount / thoughtsTokenCount

    Gemini's thinking tokens are counted as OUTPUT because that is how they bill.
    Leaving them out understates short-answer steps by orders of magnitude -- the
    April snippet_filter step measured 6,804,161 thinking tokens against 9,709
    visible output tokens.
    """
    total_in = total_out = 0
    for result in (results or {}).values():
        response = (result or {}).get('response') or {}

        usage = ((response.get('body') or {}).get('usage')) or {}
        if usage:  # GPT
            total_in += usage.get('prompt_tokens', 0) or 0
            total_out += usage.get('completion_tokens', 0) or 0
            continue

        meta = response.get('usageMetadata') or {}
        if meta:  # Gemini
            total_in += meta.get('promptTokenCount', 0) or 0
            total_out += (meta.get('candidatesTokenCount', 0) or 0) \
                + (meta.get('thoughtsTokenCount', 0) or 0)

    return total_in, total_out


@dataclass
class BatchJobState:
    """State of a batch job for persistence and resumption."""
    job_id: str
    provider: Literal["gpt", "gemini"]
    status: str
    created_at: str
    input_file_path: str
    step_name: str  # e.g., "fulltext_filter_1_2", "extract_all_prompts"
    output_file_path: Optional[str] = None
    error_file_path: Optional[str] = None
    request_count: int = 0
    completed_count: int = 0
    failed_count: int = 0
    results_cache_path: Optional[str] = None  # Path to cached results JSON
    # Token accounting. The April 2026 cost overrun could not be quantified after the
    # fact because nothing recorded usage anywhere -- no tokens, no dollars, in any log
    # or state file. Populated from provider usage metadata in retrieve_results().
    model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    metadata: Dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class BatchStateManager:
    """Manages persistent state for batch jobs using a single pipeline state file."""
    
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.pipeline_state_file = self.state_dir / "pipeline_batch_state.json"
        self.results_dir = Path("pipeline/data/batch_results")
        self.results_dir.mkdir(parents=True, exist_ok=True)
    
    def _load_pipeline_state(self) -> Dict[str, Dict]:
        """Load the pipeline-wide batch state."""
        if not self.pipeline_state_file.exists():
            return {}
        
        with open(self.pipeline_state_file, 'r', encoding="utf-8") as f:
            return json.load(f)
    
    def _save_pipeline_state(self, state: Dict[str, Dict]):
        """Save the pipeline-wide batch state."""
        with open(self.pipeline_state_file, 'w', encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    
    def save_state(self, state: BatchJobState):
        """Save batch job state for a specific step."""
        pipeline_state = self._load_pipeline_state()
        pipeline_state[state.step_name] = asdict(state)
        self._save_pipeline_state(pipeline_state)
        logger.debug(f"Saved batch state for step '{state.step_name}'")
    
    def load_state_for_step(self, step_name: str) -> Optional[BatchJobState]:
        """Load batch job state for a specific step."""
        pipeline_state = self._load_pipeline_state()
        step_data = pipeline_state.get(step_name)
        
        if not step_data:
            return None
        
        return BatchJobState(**step_data)
    
    def get_cached_results(self, step_name: str) -> Optional[Dict[str, Dict]]:
        """Get cached results for a step if they exist."""
        state = self.load_state_for_step(step_name)
        if not state or not state.results_cache_path:
            return None
        
        cache_path = Path(state.results_cache_path)
        if not cache_path.exists():
            return None
        
        try:
            with open(cache_path, 'r', encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load cached results from {cache_path}: {e}")
            return None
    
    def save_cached_results(self, step_name: str, results: Dict[str, Dict]) -> Path:
        """Save results to cache and update state."""
        cache_path = self.results_dir / f"{step_name}_results.json"
        with open(cache_path, 'w', encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        
        # Update state with cache path
        state = self.load_state_for_step(step_name)
        if state:
            state.results_cache_path = Path(cache_path).as_posix()
            self.save_state(state)
        
        logger.info(f"Cached results for step '{step_name}' to {cache_path}")
        return cache_path
    
    def list_pending_steps(self) -> List[str]:
        """List all steps with pending batch jobs."""
        pipeline_state = self._load_pipeline_state()
        pending_steps = []
        
        for step_name, step_data in pipeline_state.items():
            try:
                state = BatchJobState(**step_data)
                
                # Determine completed states based on provider
                if state.provider == "gpt":
                    completed_states = {"completed", "failed", "cancelled", "expired"}
                else:
                    completed_states = {
                        "JOB_STATE_SUCCEEDED",
                        "JOB_STATE_FAILED",
                        "JOB_STATE_CANCELLED",
                        "JOB_STATE_EXPIRED",
                    }
                
                if state.status not in completed_states:
                    pending_steps.append(step_name)
            except Exception as e:
                logger.warning(f"Failed to parse state for step {step_name}: {e}")
        
        return pending_steps
    
    def clear_step(self, step_name: str):
        """Clear state for a specific step."""
        pipeline_state = self._load_pipeline_state()
        if step_name in pipeline_state:
            del pipeline_state[step_name]
            self._save_pipeline_state(pipeline_state)
            logger.debug(f"Cleared batch state for step '{step_name}'")
    
    def clear_all(self):
        """Clear all batch state (for cleanup or reset)."""
        if self.pipeline_state_file.exists():
            self.pipeline_state_file.unlink()
            logger.info("Cleared all pipeline batch state")


class GPTBatchManager:
    """Manages batch operations for OpenAI GPT API."""
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.client = OpenAI(api_key=self.api_key)
    
    def create_batch_file(self, requests: List[Dict], output_path: Path) -> Path:
        """Create a JSONL batch input file for GPT.
        
        Args:
            requests: List of request dicts with 'custom_id', 'system_prompt', 'user_prompt', 'model', etc.
            output_path: Path to write the JSONL file
            
        Returns:
            Path to the created JSONL file
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w', encoding="utf-8", newline="\n") as f:  # LF: lines sized as +1 byte
            for req in requests:
                sys_prompt = req.get('system_prompt', '')
                usr_prompt = req.get('user_prompt', '')

                # OpenAI requires that at least one message explicitly mentions "json" when
                # using response_format={"type": "json_object"}.
                try:
                    rf = req.get('response_format')
                    rf_is_json_object = isinstance(rf, dict) and rf.get('type') == 'json_object'
                except Exception:
                    rf_is_json_object = False

                if rf_is_json_object:
                    # OpenAI's json_object response_format is picky: ensure the literal
                    # substring 'json' appears in at least one message.
                    if ('json' not in (sys_prompt or '')) and ('json' not in (usr_prompt or '')):
                        sys_prompt = (sys_prompt + "\n\nReturn valid json.").strip()

                # Build GPT batch format. `attachments` are (filename, base64 PDF) pairs, sent
                # as file parts as run_gpt does (base64 so the request stays JSON-sized).
                user_content = usr_prompt or ''
                if req.get('attachments'):
                    user_content = [{"type": "file", "file": {"filename": name,
                                                             "file_data": "data:application/pdf;base64," + b64}}
                                    for name, b64 in req['attachments']] + [{"type": "text", "text": usr_prompt or ''}]
                messages = [
                    {"role": "system", "content": sys_prompt or ''},
                    {"role": "user", "content": user_content}
                ]
                
                body = {
                    "model": req.get('model', 'gpt-6-luna'),
                    "messages": messages
                }
                
                # Add optional parameters
                if 'response_format' in req:
                    body['response_format'] = req['response_format']
                # gpt-5/gpt-6 reject any temperature but the default, and the
                # rejection fails the whole line. Same rule as run_gpt.
                if 'temperature' in req and not body['model'].lower().startswith(("gpt-5", "gpt-6")):
                    body['temperature'] = req['temperature']
                if 'max_tokens' in req:
                    body['max_tokens'] = req['max_tokens']
                
                batch_line = {
                    "custom_id": req['custom_id'],
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": body
                }
                
                f.write(json.dumps(batch_line) + '\n')
        
        logger.info(f"Created GPT batch file with {len(requests)} requests at {output_path}")
        return output_path
    
    def upload_batch_file(self, file_path: Path) -> str:
        """Upload batch file to OpenAI and return file ID."""
        with open(file_path, 'rb') as f:
            batch_input_file = self.client.files.create(
                file=f,
                purpose="batch"
            )
        logger.info(f"Uploaded batch file {file_path}, got file ID: {batch_input_file.id}")
        return batch_input_file.id
    
    def create_batch(self, file_id: str, metadata: Optional[Dict] = None) -> str:
        """Create a batch job and return batch ID."""
        batch_params = {
            "input_file_id": file_id,
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h"
        }
        
        if metadata:
            batch_params["metadata"] = metadata
        
        batch = self.client.batches.create(**batch_params)
        logger.info(f"Created GPT batch job: {batch.id}")
        return batch.id
    
    def get_batch_status(self, batch_id: str) -> Dict:
        """Get current status of a batch job."""
        batch = self.client.batches.retrieve(batch_id)
        
        return {
            'id': batch.id,
            'status': batch.status,
            'request_counts': {
                'total': batch.request_counts.total,
                'completed': batch.request_counts.completed,
                'failed': batch.request_counts.failed
            },
            'output_file_id': batch.output_file_id,
            'error_file_id': batch.error_file_id,
            'created_at': batch.created_at,
            'completed_at': batch.completed_at,
            'failed_at': batch.failed_at,
            'expired_at': batch.expired_at
        }
    
    def download_results(self, file_id: str, output_path: Path) -> Path:
        """Download batch results file."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        file_response = self.client.files.content(file_id)
        with open(output_path, 'w', encoding="utf-8", newline="\n") as f:  # as downloaded
            f.write(file_response.text)
        
        logger.info(f"Downloaded batch results to {output_path}")
        return output_path
    
    def parse_results(self, results_path: Path) -> Dict[str, Dict]:
        """Parse batch results JSONL file into a dict keyed by custom_id."""
        results = {}
        with open(results_path, 'r', encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    result = json.loads(line)
                    custom_id = result.get('custom_id')
                    if custom_id:
                        results[custom_id] = result
        return results
    
    def cancel_batch(self, batch_id: str):
        """Cancel a running batch job."""
        self.client.batches.cancel(batch_id)
        logger.info(f"Cancelled batch job: {batch_id}")


class GeminiBatchManager:
    """Manages batch operations for Google Gemini API."""
    
    def __init__(self, api_key: Optional[str] = None):
        if genai is None:
            raise ImportError("google-generativeai package not installed")
        
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        self.client = genai.Client(api_key=self.api_key)
    
    def create_batch_file(self, requests: List[Dict], output_path: Path) -> Path:
        """Create a JSONL batch input file for Gemini.
        
        Args:
            requests: List of request dicts with 'custom_id', 'system_prompt', 'user_prompt', 'model', etc.
            output_path: Path to write the JSONL file
            
        Returns:
            Path to the created JSONL file
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w', encoding="utf-8", newline="\n") as f:  # LF: lines sized as +1 byte
            for req in requests:
                # Build Gemini batch format: each line is a GenerateContentRequest
                contents = [{
                    "parts": [{"inline_data": {"mime_type": "application/pdf", "data": b64}}
                              for _name, b64 in req.get('attachments') or []]
                             + [{"text": req.get('user_prompt', '')}],
                    "role": "user"
                }]

                request_body: Dict[str, object] = {
                    "contents": contents
                }

                # Add generation_config if needed (temperature, JSON output, etc.)
                generation_config: Dict[str, object] = {}
                if 'temperature' in req:
                    generation_config['temperature'] = req['temperature']
                if 'response_format' in req:
                    # For JSON object responses, request JSON MIME type
                    if req['response_format'].get('type') == 'json_object':
                        generation_config['response_mime_type'] = 'application/json'

                # Thinking budget. Gemini 3 models think by default and bill those
                # tokens as output, which dominates cost on short-answer steps: the
                # April 26 snippet_filter run spent 656.6 thinking tokens per call to
                # emit 0.9 tokens of "true"/"false" -- 701x the visible output, and
                # $21.28 instead of $0.87 for that one step. Pass thinking_budget=0
                # on gate-style requests.
                if req.get('thinking_budget') is not None:
                    generation_config['thinking_config'] = {
                        'thinking_budget': int(req['thinking_budget'])
                    }

                if generation_config:
                    request_body['generation_config'] = generation_config

                # Add system_instruction if provided
                if req.get('system_prompt'):
                    request_body['system_instruction'] = {
                        'parts': [{'text': req['system_prompt']}]
                    }

                batch_line = {
                    "key": req['custom_id'],
                    "request": request_body
                }

                f.write(json.dumps(batch_line) + '\n')
        
        logger.info(f"Created Gemini batch file with {len(requests)} requests at {output_path}")
        return output_path
    
    def upload_batch_file(self, file_path: Path) -> str:
        """Upload batch file to Gemini and return file name.
        
        Pads file to meet Gemini's 8MB chunk size requirement for batch uploads.
        """
        import os
        import tempfile
        
        # Get file size
        file_size = os.path.getsize(file_path)
        CHUNK_SIZE = 8 * 1024 * 1024  # 8MB chunks required by Gemini
        
        # If file size is not a multiple of CHUNK_SIZE, pad it
        if file_size % CHUNK_SIZE != 0:
            # Calculate padding needed
            padding_needed = CHUNK_SIZE - (file_size % CHUNK_SIZE)
            
            # Create a temporary padded file
            with tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.jsonl') as tmp_file:
                # Copy original content
                with open(file_path, 'rb') as f:
                    tmp_file.write(f.read())
                
                # Add padding (newlines are safe for JSONL)
                tmp_file.write(b'\n' * padding_needed)
                tmp_file_path = tmp_file.name
            
            try:
                # Upload the padded file
                uploaded_file = self.client.files.upload(
                    file=tmp_file_path,
                    config={'display_name': file_path.name, 'mime_type': 'application/x-jsonlines'}
                )
                logger.info(f"Uploaded batch file {file_path} ({file_size} bytes, padded to {file_size + padding_needed} bytes), got file name: {uploaded_file.name}")
            finally:
                # Clean up temporary file
                os.unlink(tmp_file_path)
        else:
            # File size is already a multiple of CHUNK_SIZE
            uploaded_file = self.client.files.upload(
                file=str(file_path),
                config={'display_name': file_path.name, 'mime_type': 'application/x-jsonlines'}
            )
            logger.info(f"Uploaded batch file {file_path} ({file_size} bytes), got file name: {uploaded_file.name}")
        
        return uploaded_file.name
    
    def create_batch(self, file_name: str, model: str = "gemini-3-flash-preview", metadata: Optional[Dict] = None) -> str:
        """Create a batch job and return batch job name."""
        config = {'display_name': metadata.get('description', 'batch-job') if metadata else 'batch-job'}
        
        batch_job = self.client.batches.create(
            model=model,
            src=file_name,
            config=config
        )
        logger.info(f"Created Gemini batch job: {batch_job.name}")
        return batch_job.name
    
    def get_batch_status(self, batch_name: str) -> Dict:
        """Get current status of a batch job."""
        batch_job = self.client.batches.get(name=batch_name)
        
        # Extract counts if available
        total = 0
        completed = 0
        failed = 0
        
        if hasattr(batch_job, 'batch_stats'):
            stats = batch_job.batch_stats
            total = getattr(stats, 'total_request_count', 0)
            completed = getattr(stats, 'succeeded_request_count', 0)
            failed = getattr(stats, 'failed_request_count', 0)
        
        return {
            'id': batch_job.name,
            'status': batch_job.state.name,
            'request_counts': {
                'total': total,
                'completed': completed,
                'failed': failed
            },
            'output_file_name': getattr(batch_job.dest, 'file_name', None) if hasattr(batch_job, 'dest') else None,
            'created_at': None,  # Gemini doesn't expose timestamps in the same way
            'completed_at': None,
            'failed_at': None
        }
    
    def download_results(self, file_name: str, output_path: Path) -> Path:
        """Download batch results file."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        file_content = self.client.files.download(file=file_name)
        with open(output_path, 'wb') as f:
            f.write(file_content)
        
        logger.info(f"Downloaded Gemini batch results to {output_path}")
        return output_path
    
    def parse_results(self, results_path: Path) -> Dict[str, Dict]:
        """Parse Gemini batch results JSONL file into a dict keyed by custom_id (key)."""
        results = {}
        with open(results_path, 'r', encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    result = json.loads(line)
                    key = result.get('key')
                    if key:
                        results[key] = result
        return results
    
    def cancel_batch(self, batch_name: str):
        """Cancel a running batch job."""
        self.client.batches.cancel(name=batch_name)
        logger.info(f"Cancelled Gemini batch job: {batch_name}")


class BatchProcessor:
    """High-level batch processor that handles both GPT and Gemini."""
    
    def __init__(self, 
                 provider: Literal["gpt", "gemini"],
                 state_dir: Path = Path("pipeline/data/batch_state")):
        self.provider = provider
        self.state_manager = BatchStateManager(state_dir)
        
        if provider == "gpt":
            self.manager = GPTBatchManager()
        elif provider == "gemini":
            self.manager = GeminiBatchManager()
        else:
            raise ValueError(f"Unknown provider: {provider}")
    
    def create_and_submit_batch(self,
                                requests: List[Dict],
                                step_name: str,
                                model: Optional[str] = None,
                                metadata: Optional[Dict] = None) -> BatchJobState:
        """Create batch file, upload, and submit batch job.
        
        Args:
            requests: List of request dicts
            step_name: Name of the pipeline step (e.g., 'fulltext_filter_1_2', 'extract_all_prompts')
            model: Model to use (for Gemini)
            metadata: Optional metadata dict
            
        Returns:
            BatchJobState object
        """
        # Create batch file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        input_file = Path(f"pipeline/data/batch_inputs/{step_name}_{timestamp}.jsonl")
        self.manager.create_batch_file(requests, input_file)
        
        # Upload file
        file_id = self.manager.upload_batch_file(input_file)
        
        # Create batch job
        if self.provider == "gemini" and model:
            batch_id = self.manager.create_batch(file_id, model=model, metadata=metadata)
        else:
            batch_id = self.manager.create_batch(file_id, metadata=metadata)
        
        # Create state
        state = BatchJobState(
            job_id=batch_id,
            provider=self.provider,
            status='validating' if self.provider == 'gpt' else 'JOB_STATE_PENDING',
            created_at=datetime.now().isoformat(),
            input_file_path=Path(input_file).as_posix(),
            step_name=step_name,
            request_count=len(requests),
            metadata=metadata or {}
        )
        
        self.state_manager.save_state(state)
        logger.info(f"Created and submitted batch job for step '{step_name}': {batch_id}")
        return state
    
    def poll_until_complete(self,
                           step_name: str,
                           poll_interval: int = 30,
                           max_wait_time: Optional[int] = None) -> BatchJobState:
        """Poll batch job until completion or timeout.
        
        Args:
            step_name: Pipeline step name
            poll_interval: Seconds between polls
            max_wait_time: Maximum seconds to wait (None = no limit)
            
        Returns:
            Final BatchJobState
        """
        # Load state for this step
        state = self.state_manager.load_state_for_step(step_name)
        if not state:
            raise ValueError(f"No state found for step '{step_name}'")
        
        job_id = state.job_id
        
        start_time = time.time()
        # Optional coarse-grained progress logging (default every 5 minutes)
        try:
            progress_log_interval = int(os.getenv('WSI_BATCH_PROGRESS_LOG_SECS', '300'))
        except ValueError:
            progress_log_interval = 300
        last_progress_log = 0.0
        completed_states_gpt = {'completed', 'failed', 'cancelled', 'expired'}
        completed_states_gemini = {'JOB_STATE_SUCCEEDED', 'JOB_STATE_FAILED', 
                                   'JOB_STATE_CANCELLED', 'JOB_STATE_EXPIRED'}
        completed_states = completed_states_gpt if self.provider == 'gpt' else completed_states_gemini
        
        logger.warning(f"Polling batch job {job_id} (provider: {self.provider})")
        
        while state.status not in completed_states:
            # Check timeout
            if max_wait_time and (time.time() - start_time) > max_wait_time:
                logger.warning(f"Polling timeout reached for job {job_id}")
                break

            # Get status — tolerate transient API errors (5xx, network blips) so a
            # multi-hour batch isn't killed by a momentary upstream outage.
            try:
                status_info = self.manager.get_batch_status(job_id)
            except Exception as e:
                logger.warning(
                    f"Transient error polling batch {job_id}: {type(e).__name__}: {e}. "
                    f"Sleeping {poll_interval}s and retrying."
                )
                time.sleep(poll_interval)
                continue
            state.status = status_info['status']
            state.completed_count = status_info['request_counts']['completed']
            state.failed_count = status_info['request_counts']['failed']
            
            # Update output file info
            if self.provider == 'gpt':
                if status_info.get('output_file_id'):
                    state.output_file_path = status_info['output_file_id']
                if status_info.get('error_file_id'):
                    state.error_file_path = status_info['error_file_id']
            else:  # gemini
                if status_info.get('output_file_name'):
                    state.output_file_path = status_info['output_file_name']
            
            # Save updated state
            self.state_manager.save_state(state)
            
            # Log progress
            logger.info(f"Batch {job_id}: {state.status} - "
                       f"{state.completed_count}/{state.request_count} completed, "
                       f"{state.failed_count} failed")

            # Coarser heartbeat log every N seconds to reassure long waits
            elapsed = time.time() - start_time
            if progress_log_interval > 0 and (elapsed - last_progress_log) >= progress_log_interval:
                logger.warning(
                    "[batch-heartbeat] Job %s still running after %.1f minutes: %s (%d/%d completed, %d failed)",
                    job_id,
                    elapsed / 60.0,
                    state.status,
                    state.completed_count,
                    state.request_count,
                    state.failed_count,
                )
                last_progress_log = elapsed
            
            if state.status in completed_states:
                break
            
            # Wait before next poll
            time.sleep(poll_interval)
        
        logger.warning(f"Batch job {job_id} for step '{step_name}' finished with status: {state.status}")
        return state
    
    def retrieve_results(self, step_name: str) -> Dict[str, Dict]:
        """Retrieve and parse batch results for a step.
        
        Checks cache first, downloads if needed, then caches results.
        
        Args:
            step_name: Pipeline step name
            
        Returns:
            Dict mapping custom_id to result dict
        """
        # Check if results are already cached
        cached_results = self.state_manager.get_cached_results(step_name)
        if cached_results is not None:
            logger.info(f"Using cached results for step '{step_name}'")
            return cached_results
        
        # Load state to get job info
        state = self.state_manager.load_state_for_step(step_name)
        if not state:
            raise ValueError(f"No state found for step '{step_name}'")
        
        job_id = state.job_id

        # Some providers (notably OpenAI Batch) may mark a job as completed while
        # providing only an error file (e.g., when all requests failed). Also, the
        # persisted state can be stale if the job finished out-of-band.
        if self.provider == 'gpt' and (not state.output_file_path and not state.error_file_path):
            try:
                status_info = self.manager.get_batch_status(job_id)
                state.status = status_info.get('status', state.status)
                state.completed_count = status_info.get('request_counts', {}).get('completed', state.completed_count)
                state.failed_count = status_info.get('request_counts', {}).get('failed', state.failed_count)
                if status_info.get('output_file_id'):
                    state.output_file_path = status_info['output_file_id']
                if status_info.get('error_file_id'):
                    state.error_file_path = status_info['error_file_id']
                self.state_manager.save_state(state)
            except Exception as e:
                logger.warning(
                    "Failed to refresh batch status for job %s while retrieving results for step '%s': %s",
                    job_id,
                    step_name,
                    e,
                )

        file_id: Optional[str] = state.output_file_path
        file_kind = "output"
        if not file_id and self.provider == 'gpt' and state.error_file_path:
            file_id = state.error_file_path
            file_kind = "error"

        if not file_id and self.provider == 'gpt':
            try:
                status_info = self.manager.get_batch_status(job_id)
                state.status = status_info.get('status', state.status)
                state.completed_count = status_info.get('request_counts', {}).get('completed', state.completed_count)
                state.failed_count = status_info.get('request_counts', {}).get('failed', state.failed_count)
                if status_info.get('output_file_id'):
                    state.output_file_path = status_info['output_file_id']
                if status_info.get('error_file_id'):
                    state.error_file_path = status_info['error_file_id']
                self.state_manager.save_state(state)

                file_id = state.output_file_path or state.error_file_path
                file_kind = "output" if state.output_file_path else "error"
            except Exception as e:
                logger.warning(
                    "Failed to refresh batch status for job %s while selecting results file for step '%s': %s",
                    job_id,
                    step_name,
                    e,
                )

        if not file_id:
            logger.warning(
                "No output or error file available for job %s (step '%s', status '%s', completed=%d, failed=%d). Returning empty results.",
                job_id,
                step_name,
                state.status,
                state.completed_count,
                state.failed_count,
            )
            self.state_manager.save_cached_results(step_name, {})
            return {}
        
        # Download results
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = "" if file_kind == "output" else "_error"
        output_path = Path(f"pipeline/data/batch_outputs/{job_id}_{timestamp}{suffix}.jsonl")
        
        if self.provider == 'gpt':
            if file_kind == "error":
                logger.warning(
                    "Batch job %s for step '%s' has no output file; downloading error file instead",
                    job_id,
                    step_name,
                )
            self.manager.download_results(file_id, output_path)
        else:  # gemini
            self.manager.download_results(file_id, output_path)
        
        # Parse results
        results = self.manager.parse_results(output_path)
        logger.info(f"Retrieved {len(results)} results for step '{step_name}'")

        # Record token usage so a run's spend is knowable afterwards.
        try:
            state.input_tokens, state.output_tokens = _sum_usage(results)
            if state.input_tokens or state.output_tokens:
                logger.info(
                    "Step '%s' token usage: %s in / %s out (model=%s)",
                    step_name, f"{state.input_tokens:,}", f"{state.output_tokens:,}",
                    state.model or "unknown",
                )
                self.state_manager.save_state(state)
        except Exception as e:  # never let accounting break a completed batch
            logger.warning("Could not record token usage for step '%s': %s", step_name, e)

        # Cache results
        self.state_manager.save_cached_results(step_name, results)

        return results
    
    def get_or_create_batch(self,
                           requests: List[Dict],
                           step_name: str,
                           model: Optional[str] = None,
                           metadata: Optional[Dict] = None) -> Dict[str, Dict]:
        """Get cached results or create and process batches for a step.

        Splits oversized request sets into sub-batches (see ``split_requests``) and
        merges their results. Each sub-batch carries its own step name, so state,
        caching and resume all work per chunk -- a failure part-way through only
        costs the chunks that hadn't finished.

        Args:
            requests: List of request dicts
            step_name: Pipeline step name
            model: Model to use (optional)
            metadata: Optional metadata

        Returns:
            Dict mapping custom_id to result dict
        """
        chunks = split_requests(requests)

        # Single chunk keeps the bare step name, so pre-existing cached results and
        # in-flight state from before splitting existed remain valid.
        if len(chunks) <= 1:
            return self._get_or_create_single_batch(requests, step_name, model, metadata)

        total_bytes = sum(len(json.dumps(r).encode("utf-8")) + 1 for r in requests)
        logger.info(
            "Step '%s': %d requests (~%.1f MB) exceeds the per-batch limit "
            "(%d requests / %.0f MB); splitting into %d sub-batches",
            step_name, len(requests), total_bytes / 1e6,
            MAX_REQUESTS_PER_BATCH, MAX_BATCH_BYTES / 1e6, len(chunks),
        )

        # Submit every sub-batch BEFORE polling any of them. Submitting and polling
        # each chunk in turn would serialise N batch latencies -- with the ~2.5 h a
        # large Gemini batch took in April, a step split into tens of chunks would
        # take days instead of hours. The provider runs them concurrently; we just
        # have to hand them all over first.
        merged: Dict[str, Dict] = {}
        pending: List[str] = []

        for idx, chunk in enumerate(chunks, start=1):
            part_name = f"{step_name}__part{idx}of{len(chunks)}"
            part_meta = dict(metadata or {})
            part_meta.update({
                'description': f"{part_meta.get('description', step_name)} [{idx}/{len(chunks)}]",
                'parent_step': step_name,
                'part': idx,
                'parts': len(chunks),
            })

            cached = self._cached_if_complete(chunk, part_name)
            if cached is not None:
                logger.info("Step '%s': sub-batch %d/%d already cached (%d items)",
                            step_name, idx, len(chunks), len(cached))
                merged.update(cached)
                continue

            existing = self.state_manager.load_state_for_step(part_name)
            if existing and existing.provider == self.provider:
                logger.info("Step '%s': sub-batch %d/%d already in flight (%s)",
                            step_name, idx, len(chunks), existing.job_id)
            else:
                logger.info("Step '%s': submitting sub-batch %d/%d (%d requests)",
                            step_name, idx, len(chunks), len(chunk))
                self.create_and_submit_batch(chunk, part_name, model, part_meta)
            pending.append(part_name)

        if pending:
            logger.info("Step '%s': %d sub-batches in flight; now polling",
                        step_name, len(pending))
        for part_name in pending:
            try:
                self.poll_until_complete(part_name)
                merged.update(self.retrieve_results(part_name))
            except Exception as e:
                logger.error("Step '%s': sub-batch '%s' failed: %s. Its results are "
                             "missing from this step; rerun to retry just this chunk.",
                             step_name, part_name, e)
                self.state_manager.clear_step(part_name)

        expected = {str(r.get('custom_id')) for r in requests if r.get('custom_id') is not None}
        missing = expected - merged.keys()
        if missing:
            logger.warning(
                "Step '%s': %d/%d custom_ids missing after merging %d sub-batches (e.g. %s)",
                step_name, len(missing), len(expected), len(chunks), sorted(missing)[:5],
            )
        return merged

    def _cached_if_complete(self, requests: List[Dict], step_name: str) -> Optional[Dict[str, Dict]]:
        """Cached results for a step, but only if they cover every requested id.

        The cache is keyed by step name alone, so a changed input set can leave it
        covering the wrong ids. Returns None (and clears the stale state) in that case.
        """
        cached = self.state_manager.get_cached_results(step_name)
        if cached is None:
            return None
        try:
            requested = {str(r.get('custom_id')) for r in (requests or [])
                         if r.get('custom_id') is not None}
            missing = sorted(i for i in requested if i not in cached)
        except Exception:
            return cached
        if missing:
            logger.warning(
                "Cached results for step '%s' are missing %d/%d requested ids (e.g., %s). "
                "Ignoring cache and creating a new batch.",
                step_name, len(missing), len(requested), missing[:5],
            )
            self.state_manager.clear_step(step_name)
            return None
        return cached

    def _get_or_create_single_batch(self,
                                    requests: List[Dict],
                                    step_name: str,
                                    model: Optional[str] = None,
                                    metadata: Optional[Dict] = None) -> Dict[str, Dict]:
        """Run exactly one batch: cache check, resume, submit, poll, retrieve."""
        # Check for cached results first
        cached_results = self.state_manager.get_cached_results(step_name)
        if cached_results is not None:
            try:
                requested_ids = {str(r.get('custom_id')) for r in (requests or []) if r.get('custom_id') is not None}
                missing_ids = sorted(i for i in requested_ids if i not in cached_results)
            except Exception:
                requested_ids = set()
                missing_ids = []

            if missing_ids:
                # The cache is keyed only by step name. If the input set changes between runs,
                # cached results may not contain entries for newly requested IDs.
                logger.warning(
                    "Cached results for step '%s' are missing %d/%d requested ids (e.g., %s). "
                    "Ignoring cache and creating a new batch.",
                    step_name,
                    len(missing_ids),
                    len(requested_ids),
                    missing_ids[:5],
                )
                # Clear stale state so subsequent calls won't keep reusing an incompatible cache.
                self.state_manager.clear_step(step_name)
            else:
                logger.info(f"Using cached results for step '{step_name}' ({len(cached_results)} items)")
                return cached_results
        
        # Check if there's a pending job for this step
        state = self.state_manager.load_state_for_step(step_name)
        
        if state and state.provider == self.provider:
            # Resume existing job
            logger.info(f"Resuming pending batch job for step '{step_name}': {state.job_id}")
            try:
                final_state = self.poll_until_complete(step_name)
                return self.retrieve_results(step_name)
            except Exception as e:
                logger.error(f"Failed to resume job for step '{step_name}': {e}")
                # Clear failed state and create new job
                self.state_manager.clear_step(step_name)
        
        # Create new batch job
        logger.info(f"Creating new batch job for step '{step_name}' with {len(requests)} requests")
        state = self.create_and_submit_batch(
            requests=requests,
            step_name=step_name,
            model=model,
            metadata=metadata
        )
        
        # Poll until complete
        final_state = self.poll_until_complete(step_name)
        
        # Retrieve and cache results
        return self.retrieve_results(step_name)
