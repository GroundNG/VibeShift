# /src/llm/lm_client.py
from google import genai
from PIL import Image
import io
import logging
import time # Import time module
import threading # Import threading for lock
from typing import Type, Optional, Union, List, Dict, Any
logger = logging.getLogger(__name__)
import base64
import json 

from .clients.gemini_client import GeminiClient
from .clients.azure_openai_client import AzureOpenAIClient
from .clients.openai_client import OpenAIClient


class LLMClient:
    """
    Handles interactions with LLM APIs (Google Gemini or any LLM with OpenAI sdk)
    with rate limiting.
    """

    # Rate limiting parameters (adjust based on the specific API limits)
    # Consider making this provider-specific if needed
    MIN_REQUEST_INTERVAL_SECONDS = 3.0 # Adjusted slightly, Gemini free is 15 RPM (4s), LLM depends on tier

    def __init__(self, provider: str):# 'gemini' or 'LLM'
        """
        Initializes the LLM client for the specified provider.

        Args:
            provider: The LLM provider to use ('gemini' or 'openai' or 'azure').
        """
        self.provider = provider.lower()
        self.client = None

        if self.provider == 'gemini':
            self.client = GeminiClient()
        elif self.provider == 'openai':
            self.client = OpenAIClient()
        elif self.provider == 'azure':
            self.client = AzureOpenAIClient()
        else:
            raise ValueError(f"Unsupported provider: {provider}. Choose 'gemini' or 'openai' or 'azure'.")
        
        # Common initialization
        self._last_request_time = 0.0
        self._lock = threading.Lock() # Lock for rate limiting
        logger.info(f"LLMClient initialized for provider '{self.provider}' with {self.MIN_REQUEST_INTERVAL_SECONDS}s request interval.")


    def _wait_for_rate_limit(self):
        """Waits if necessary to maintain the minimum request interval."""
        with self._lock: # Ensure thread-safe access
            now = time.monotonic()
            elapsed = now - self._last_request_time
            wait_time = self.MIN_REQUEST_INTERVAL_SECONDS - elapsed

            if wait_time > 0:
                logger.debug(f"Rate limiting: Waiting for {wait_time:.2f} seconds...")
                time.sleep(wait_time)

            self._last_request_time = time.monotonic() # Update after potential wait

    def generate_text(self, prompt: str) -> str:
          """Generates text using the configured LLM provider, respecting rate limits."""
          self._wait_for_rate_limit() # Wait before making the API call
          return self.client.generate_text(prompt)


    def generate_multimodal(self, prompt: str, image_bytes: bytes) -> str:
          """Generates text based on a prompt and an image, respecting rate limits."""
          self._wait_for_rate_limit() # Wait before making the API call
          return self.client.generate_multimodal(prompt, image_bytes)

    def generate_json(self, Schema_Class: Type, prompt: str, image_bytes: Optional[bytes] = None) -> Union[Dict[str, Any], str]:
          """
          Generates structured JSON output based on a prompt, an optional image,
          and a defined schema, respecting rate limits.

          For Gemini, Schema_Class should be a Pydantic BaseModel or compatible type.
          For any other LLM, Schema_Class must be a Pydantic BaseModel.

          Returns:
              A dictionary representing the parsed JSON on success, or an error string.
          """
          self._wait_for_rate_limit()
          return self.client.generate_json(Schema_Class, prompt, image_bytes)