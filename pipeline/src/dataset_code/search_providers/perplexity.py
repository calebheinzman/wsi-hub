"""Perplexity AI search provider for dataset discovery."""
import os
import time
import random
import logging
import requests
from typing import Dict

logger = logging.getLogger(__name__)


def search_perplexity(dataset_name: str, query_prompt: str = None) -> Dict[str, str]:
    """
    Search using Perplexity AI API.
    
    Args:
        dataset_name: Name of the dataset to search for
        query_prompt: Optional custom prompt template
        
    Returns:
        Dict with 'content' key containing the search response
    """
    api_key = os.getenv("PERPLEXITY_API_KEY", "")
    if not api_key:
        logger.warning("PERPLEXITY_API_KEY not set, skipping Perplexity search")
        return {"content": "", "error": "API key not set"}
    
    url = "https://api.perplexity.ai/chat/completions"
    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {api_key}",
        "content-type": "application/json"
    }
    
    # Default prompt if none provided
    if query_prompt is None:
        query_prompt = (
            f"Provide detailed information about the {dataset_name} dataset. "
            f"Include: description, download link, tissue types, image format, "
            f"number of images/slides, annotations, license, and any technical specifications. "
            f"Focus on pathology and whole slide imaging datasets."
        )
    else:
        query_prompt = query_prompt.format(dataset_name=dataset_name)
    
    payload = {
        "model": "sonar-pro",
        "messages": [
            {"role": "user", "content": query_prompt}
        ],
    }
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=60)
            
            if response.status_code == 429:
                # Rate limit - exponential backoff
                sleep_time = min(2.0 * (2 ** attempt), 10.0) + random.random() * 0.5
                logger.warning(f"Perplexity rate limit hit, sleeping {sleep_time:.2f}s")
                time.sleep(sleep_time)
                continue
            
            response.raise_for_status()
            data = response.json()
            
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            
            logger.info(f"Perplexity search returned {len(content)} chars for dataset: {dataset_name}")
            return {
                "content": content,
                "provider": "perplexity",
                "dataset_name": dataset_name
            }
            
        except requests.exceptions.RequestException as e:
            logger.warning(f"Perplexity search attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                sleep_time = min(2.0 * (2 ** attempt), 10.0) + random.random() * 0.5
                time.sleep(sleep_time)
            else:
                logger.error(f"Perplexity search failed after {max_retries} attempts")
                return {"content": "", "error": str(e)}
    
    return {"content": "", "error": "Max retries exceeded"}
