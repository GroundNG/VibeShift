# /src/llm/clients/gemini_client.py
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

from ...utils.utils import load_api_key

class GeminiClient:
    def __init__(self):
        self.client = None
        gemini_api_key = load_api_key()
        if not gemini_api_key:
            raise ValueError("gemini_api_key is required for provider 'gemini'")
        try:
            # genai.configure(api_key=gemini_api_key) # configure is global, prefer Client
            self.client = genai.Client(api_key=gemini_api_key)
            # Test connection slightly by listing models (optional)
            # list(self.client.models.list())
            logger.info("Google Gemini Client initialized.")
        except Exception as e:
            logger.error(f"Failed to initialize Google Gemini Client: {e}", exc_info=True)
            raise RuntimeError(f"Gemini client initialization failed: {e}")
        
    
    def generate_text(self, prompt: str) -> str:
        """Generates text using the Gemini text model, respecting rate limits."""
        try:
            # Truncate prompt for logging if too long
            log_prompt = prompt[:200] + ('...' if len(prompt) > 200 else '')
            logger.debug(f"Sending text prompt (truncated): {log_prompt}")
            # response = self.text_model.generate_content(prompt)
            response = self.client.models.generate_content(
                        model='gemini-2.0-flash',
                        contents=prompt
                )
            logger.debug("Received text response.")

            # Improved response handling
            if hasattr(response, 'text'):
                return response.text
            elif response.parts:
                # Sometimes response might be in parts without direct .text attribute
                return "".join(part.text for part in response.parts if hasattr(part, 'text'))
            elif response.prompt_feedback and response.prompt_feedback.block_reason:
                block_reason = response.prompt_feedback.block_reason
                block_message = f"Error: Content generation blocked due to {block_reason}"
                if response.prompt_feedback.safety_ratings:
                        block_message += f" - Safety Ratings: {response.prompt_feedback.safety_ratings}"
                logger.warning(block_message)
                return block_message
            else:
                logger.warning(f"Text generation returned no text/parts and no block reason. Response: {response}")
                return "Error: Empty or unexpected response from LLM."

        except Exception as e:
            logger.error(f"Error during Gemini text generation: {e}", exc_info=True)
            return f"Error: Failed to communicate with Gemini API - {type(e).__name__}: {e}"
        
    def generate_multimodal(self, prompt: str, image_bytes: bytes) -> str:
          """Generates text based on a prompt and an image, respecting rate limits."""
          try:
               log_prompt = prompt[:200] + ('...' if len(prompt) > 200 else '')
               #   logger.debug(f"Sending multimodal prompt (truncated): {log_prompt} with image.")
               image = Image.open(io.BytesIO(image_bytes))
               #   response = self.vision_model.generate_content([prompt, image])
               response = self.client.models.generate_content(
                    model='gemini-2.0-flash',
                    contents=[
                         prompt,
                         image
                    ]
               )
               logger.debug("Received multimodal response.")

               # Improved response handling (similar to text)
               if hasattr(response, 'text'):
                    return response.text
               elif response.parts:
                    return "".join(part.text for part in response.parts if hasattr(part, 'text'))
               elif response.prompt_feedback and response.prompt_feedback.block_reason:
                    block_reason = response.prompt_feedback.block_reason
                    block_message = f"Error: Multimodal generation blocked due to {block_reason}"
                    if response.prompt_feedback.safety_ratings:
                         block_message += f" - Safety Ratings: {response.prompt_feedback.safety_ratings}"
                    logger.warning(block_message)
                    return block_message
               else:
                    logger.warning(f"Multimodal generation returned no text/parts and no block reason. Response: {response}")
                    return "Error: Empty or unexpected response from Vision LLM."

          except Exception as e:
               logger.error(f"Error during Gemini multimodal generation: {e}", exc_info=True)
               return f"Error: Failed to communicate with Gemini Vision API - {type(e).__name__}: {e}"
          

    def generate_json(self, Schema_Class: Type, prompt: str, image_bytes: Optional[bytes] = None) -> Union[Dict[str, Any], str]:
        """generates json based on prompt and a defined schema"""
        contents = prompt
        if(image_bytes is not None):
            image = Image.open(io.BytesIO(image_bytes))
            contents = [prompt, image]
        try:
            log_prompt = prompt[:200] + ('...' if len(prompt) > 200 else '')
            logger.debug(f"Sending text prompt (truncated): {log_prompt}")
            response = self.client.models.generate_content(
                model='gemini-2.0-flash',
                contents=contents,
                config={
                        'response_mime_type': 'application/json',
                        'response_schema': Schema_Class
                }
            )
            logger.debug("Received json response from LLM")
            if hasattr(response, 'parsed'):
                return response.parsed
            elif response.prompt_feedback and response.prompt_feedback.block_reason:
                block_reason = response.prompt_feedback.block_reason
                block_message = f"Error: JSON generation blocked due to {block_reason}"
                if response.prompt_feedback.safety_ratings:
                        block_message += f" - Safety Ratings: {response.prompt_feedback.safety_ratings}"
                logger.warning(block_message)
                return block_message
            else:
                logger.warning(f"JSON generation returned no text/parts and no block reason. Response: {response}")
                return "Error: Empty or unexpected response from JSON LLM."
        except Exception as e:
            logger.error(f"Error during Gemini JSON generation: {e}", exc_info=True)
            return f"Error: Failed to communicate with Gemini JSON API - {type(e).__name__}: {e}"
