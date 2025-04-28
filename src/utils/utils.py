# /src/utils/utils.py
import os
from dotenv import load_dotenv

def load_api_key():
    """Loads the llm API key from .env file."""
    load_dotenv()
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        raise ValueError("LLM_API_KEY not found in .env file or environment variables.")
    return api_key

def load_base_url():
    """Loads the API base url from .env file."""
    load_dotenv()
    base_url = os.getenv("LLM_BASE_URL")
    if not base_url:
        raise ValueError("LLM_BASE_URL not found in .env file or environment variables.")
    return base_url

def load_llm_model():
    """Loads the llm model from .env file."""
    load_dotenv()
    llm_model = os.getenv("LLM_MODEL")
    if not llm_model:
        raise ValueError("LLM_MODEL not found in .env file or environment variables.")
    return llm_model

def load_llm_timeout():
    """Loads the default llm model timeout from .env file."""
    load_dotenv()
    llm_timeout = os.getenv("LLM_TIMEOUT")
    if not llm_timeout:
        raise ValueError("LLM_TIMEOUT not found in .env file or environment variables.")
    return llm_timeout