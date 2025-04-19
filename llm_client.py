# llm_client.py
# import google.generativeai as genai
from google import genai
from PIL import Image
import io
import logging
import time # Import time module
import threading # Import threading for lock

logger = logging.getLogger(__name__)

class GeminiClient:
     """Handles interactions with the Google Gemini API with rate limiting."""

     # Rate limiting parameters (adjust based on current Gemini free tier limits)
     # 15 RPM = 1 call every (60 / 15) = 4 seconds. Add buffer.
     MIN_REQUEST_INTERVAL_SECONDS = 6.0
     
     client = None
     
     def __init__(self, api_key: str):
     #    genai.configure(api_key=api_key)
        self.client = genai.Client(api_key=api_key)
     #    self.text_model = genai.GenerativeModel('gemini-2.0-flash')
     #    self.vision_model = genai.GenerativeModel('gemini-2.0-flash')
        self._last_request_time = 0.0
        self._lock = threading.Lock() # Lock to prevent race conditions in rate limiting
        logger.info(f"GeminiClient initialized with {self.MIN_REQUEST_INTERVAL_SECONDS}s request interval.")

     def _wait_for_rate_limit(self):
          """Waits if necessary to maintain the minimum request interval."""
          with self._lock: # Ensure thread-safe access to _last_request_time
               now = time.monotonic()
               elapsed = now - self._last_request_time
               wait_time = self.MIN_REQUEST_INTERVAL_SECONDS - elapsed

               if wait_time > 0:
                    logger.debug(f"Rate limiting: Waiting for {wait_time:.2f} seconds...")
                    time.sleep(wait_time)

               self._last_request_time = time.monotonic() # Update last request time *after* potential wait

     def generate_text(self, prompt: str) -> str:
          """Generates text using the Gemini text model, respecting rate limits."""
          self._wait_for_rate_limit() # Wait before making the API call
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
          self._wait_for_rate_limit() # Wait before making the API call
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
          
     
     def generate_json(self, Schema_Class, prompt, image_bytes=None):
          """generates json based on prompt and a defined schema"""
          self._wait_for_rate_limit()
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