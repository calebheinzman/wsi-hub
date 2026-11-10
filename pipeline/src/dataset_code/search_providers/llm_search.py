"""LLM-based search providers (GPT and Gemini) for dataset discovery."""
import os
import logging
from typing import Dict
from pipeline.utils.llms.llm import run_gpt

logger = logging.getLogger(__name__)


def search_with_llm(dataset_name: str, use_gemini: bool = False, query_prompt: str = None) -> Dict[str, str]:
    """
    Search using OpenAI GPT or Google Gemini.
    
    Args:
        dataset_name: Name of the dataset to search for
        use_gemini: If True, use Gemini; otherwise use GPT
        query_prompt: Optional custom prompt template
        
    Returns:
        Dict with 'content' key containing the LLM response
    """
    # Check for API keys
    if use_gemini:
        api_key = os.getenv("GEMINI_API_KEY", "")
        provider_name = "Gemini"
        model = "gemini-3-flash-preview"
    else:
        api_key = os.getenv("OPENAI_API_KEY", "")
        provider_name = "GPT"
        model = "gpt-6-luna"
    
    if not api_key:
        logger.warning(f"{provider_name} API key not set, skipping {provider_name} search")
        return {"content": "", "error": f"{provider_name} API key not set"}
    
    # Default prompt if none provided
    if query_prompt is None:
        system_prompt = (
            "You are a research assistant specializing in pathology and medical imaging datasets. "
            "Provide accurate, detailed information about datasets when asked."
        )
        user_prompt = (
            f"Provide comprehensive information about the {dataset_name} dataset. "
            f"Include the following details if available:\n"
            f"- Full dataset name and any aliases\n"
            f"- Description and purpose\n"
            f"- Download link or official website\n"
            f"- Tissue types and organs covered\n"
            f"- Image format (WSI format like SVS, NDPI, etc.)\n"
            f"- Number of slides/images and patients\n"
            f"- Annotation details (type, level, format)\n"
            f"- Staining type (H&E, IHC, etc.)\n"
            f"- Scanner information if available\n"
            f"- License type\n"
            f"- Year of release\n"
            f"- Associated institutions or publications\n\n"
            f"Focus specifically on pathology whole slide imaging datasets. "
            f"If this is not a pathology/WSI dataset, clearly state that."
        )
    else:
        system_prompt = "You are a research assistant specializing in pathology and medical imaging datasets."
        user_prompt = query_prompt.format(dataset_name=dataset_name)
    
    try:
        response = run_gpt(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            use_gemini=use_gemini,
            item=dataset_name,
        )
        
        content = response.choices[0].message.content or ""
        
        logger.info(f"{provider_name} search returned {len(content)} chars for dataset: {dataset_name}")
        return {
            "content": content,
            "provider": "gemini" if use_gemini else "gpt",
            "dataset_name": dataset_name
        }
        
    except Exception as e:
        logger.error(f"{provider_name} search failed for {dataset_name}: {e}")
        return {"content": "", "error": str(e)}


def search_gpt(dataset_name: str, query_prompt: str = None) -> Dict[str, str]:
    """Search using OpenAI GPT."""
    return search_with_llm(dataset_name, use_gemini=False, query_prompt=query_prompt)


def search_gemini(dataset_name: str, query_prompt: str = None) -> Dict[str, str]:
    """Search using Google Gemini."""
    return search_with_llm(dataset_name, use_gemini=True, query_prompt=query_prompt)
