"""Serper web search provider for dataset discovery."""
import os
import time
import random
import logging
import requests
from typing import List, Dict, Optional

from pipeline.utils.llms.llm import log_event
from pipeline.utils.scraping.http import polite_get

logger = logging.getLogger(__name__)


def search_serper(query: str, num_results: int = 10) -> List[Dict[str, str]]:
    """
    Search using Serper API.
    
    Args:
        query: Search query string
        num_results: Number of results to return (default 10)
        
    Returns:
        List of search results with 'title', 'link', 'snippet' keys
    """
    api_key = os.getenv("SERPER_API_KEY", "")
    if not api_key:
        logger.warning("SERPER_API_KEY not set, skipping Serper search")
        return []
    
    url = "https://google.serper.dev/search"
    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json"
    }
    
    payload = {
        "q": query,
        "num": num_results
    }
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            t0 = time.time()
            response = polite_get(url, method="POST", json_body=payload, headers=headers, timeout=30)
            log_event("serper", query=query, status=response.status_code, seconds=round(time.time() - t0, 2))
            
            if response.status_code == 429:
                # Rate limit - exponential backoff
                sleep_time = min(2.0 * (2 ** attempt), 10.0) + random.random() * 0.5
                logger.warning(f"Serper rate limit hit, sleeping {sleep_time:.2f}s")
                time.sleep(sleep_time)
                continue
            
            response.raise_for_status()
            data = response.json()
            
            results = []
            for item in data.get("organic", [])[:num_results]:
                results.append({
                    "title": item.get("title", ""),
                    "link": item.get("link", ""),
                    "snippet": item.get("snippet", "")
                })
            
            logger.info(f"Serper search returned {len(results)} results for query: {query[:50]}...")
            return results
            
        except requests.exceptions.RequestException as e:
            logger.warning(f"Serper search attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                sleep_time = min(2.0 * (2 ** attempt), 10.0) + random.random() * 0.5
                time.sleep(sleep_time)
            else:
                logger.error(f"Serper search failed after {max_retries} attempts")
                return []
    
    return []


def build_dataset_query(dataset_name: str, aliases: Optional[List[str]] = None) -> str:
    """
    Build a search query for a dataset.
    
    Args:
        dataset_name: Primary dataset name
        aliases: Optional list of known aliases
        
    Returns:
        Formatted search query string
    """
    base_query = f"{dataset_name} pathology whole slide images dataset"
    
    if aliases:
        # Add first alias if available
        base_query = f"{dataset_name} {aliases[0]} pathology WSI dataset"
    
    return base_query


def build_dataset_queries(dataset_name: str) -> List[str]:
    """Every Serper query run for a dataset, in priority order.

    The topical query alone misses datasets with ambiguous names or that are not
    whole-slide sets: for "Chaoyang" (a patch dataset, and a Beijing district) it
    returned papers and dataset lists but not the official GitHub repository. The
    exact-name query -- what the labelling tool's Google link searches -- puts that
    repository first.
    """
    return [f'"{dataset_name}" dataset', build_dataset_query(dataset_name)]
