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
# --- Provider Specific Imports ---
try:
    import openai
    from openai import AzureOpenAI
    from pydantic import BaseModel # Needed for LLM JSON tool definition
    OPENAI_SDK = True
except ImportError:
    OPENAI_SDK = False
    # Define dummy classes if LLM libs are not installed to avoid NameErrors
    class BaseModel: pass
    class OpenAI: pass




# --- Helper Function ---
def _image_bytes_to_base64_url(image_bytes: bytes) -> Optional[str]:
    """Converts image bytes to a base64 data URL."""
    try:
        # Try to determine the image format
        img = Image.open(io.BytesIO(image_bytes))
        format = img.format
        if not format:
            logger.warning("Could not determine image format, assuming JPEG.")
            format = "jpeg" # Default assumption
        else:
            format = format.lower()
            if format == 'jpg': # Standardize to jpeg
                format = 'jpeg'

        # Ensure format is supported (common web formats)
        if format not in ['jpeg', 'png', 'gif', 'webp']:
             logger.warning(f"Unsupported image format '{format}' for base64 URL, defaulting to JPEG.")
             format = 'jpeg' # Fallback

        encoded_string = base64.b64encode(image_bytes).decode('utf-8')
        return f"data:image/{format};base64,{encoded_string}"
    except Exception as e:
        logger.error(f"Error converting image bytes to base64 URL: {e}", exc_info=True)
        return None


class LLMClient:
     """
     Handles interactions with LLM APIs (Google Gemini or any LLM with OpenAI sdk)
     with rate limiting.
     """

     # Rate limiting parameters (adjust based on the specific API limits)
     # Consider making this provider-specific if needed
     MIN_REQUEST_INTERVAL_SECONDS = 3.0 # Adjusted slightly, Gemini free is 15 RPM (4s), LLM depends on tier

     def __init__(self,
                  provider: str, # 'gemini' or 'LLM'
                  gemini_api_key: Optional[str] = None,
                  LLM_api_key: Optional[str] = None,
                  LLM_endpoint: Optional[str] = None,
                  LLM_api_version: Optional[str] = None,
                  LLM_model_name: Optional[str] = None, # Default text/chat model
                  LLM_vision_model_name: Optional[str] = None # Vision-capable model
                 ):
        """
        Initializes the LLM client for the specified provider.

        Args:
            provider: The LLM provider to use ('gemini' or 'LLM').
            gemini_api_key: API key for Google Gemini. Required if provider is 'gemini'.
            LLM_api_key: API key for LLM OpenAI. Required if provider is 'LLM'.
            LLM_endpoint: Endpoint URL for LLM OpenAI. Required if provider is 'LLM'.
            LLM_api_version: API version for LLM OpenAI. Required if provider is 'LLM'.
            LLM_model_name: Deployment name for the default LLM text/chat model. Required if provider is 'LLM'.
            LLM_vision_model_name: Deployment name for the LLM vision model. If None, defaults to LLM_model_name. Required for multimodal if provider is 'LLM'.
        """
        self.provider = provider.lower()
        self.client = None
        self.LLM_model_name = LLM_model_name
        self.LLM_vision_model_name = LLM_vision_model_name or LLM_model_name # Default to main model if vision not specified

        if self.provider == 'gemini':
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

        elif self.provider == 'llm':
            if not OPENAI_SDK:
                 raise ImportError("LLM OpenAI libraries (openai, pydantic) are not installed. Please install them.")
            if not all([LLM_api_key, LLM_endpoint, LLM_api_version, LLM_model_name]):
                raise ValueError("LLM_api_key, LLM_endpoint, LLM_api_version, and LLM_model_name are required for provider 'LLM'")
            try:
                self.client = AzureOpenAI(
                    api_key=LLM_api_key,
                    # base_url=LLM_endpoint,
                    azure_endpoint=LLM_endpoint,
                    api_version=LLM_api_version
                )
                # Test connection slightly by listing models (optional, requires different permission potentially)
                # self.client.models.list()
                logger.info(f"LLM OpenAI Client initialized for endpoint {LLM_endpoint} and model {LLM_model_name}.")
            except Exception as e:
                logger.error(f"Failed to initialize LLM OpenAI Client: {e}", exc_info=True)
                raise RuntimeError(f"LLM client initialization failed: {e}")

        else:
            raise ValueError(f"Unsupported provider: {provider}. Choose 'gemini' or 'LLM'.")

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

     # --- Gemini Specific Methods ---

     def _generate_text_gemini(self, prompt: str) -> str:
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


     def _generate_multimodal_gemini(self, prompt: str, image_bytes: bytes) -> str:
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
          

     def _generate_json_gemini(self, Schema_Class: Type, prompt: str, image_bytes: Optional[bytes] = None) -> Union[Dict[str, Any], str]:
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

     # --- LLM OpenAI Specific Methods ---

     def _generate_text_LLM(self, prompt: str) -> str:
         try:
             log_prompt = prompt[:200] + ('...' if len(prompt) > 200 else '')
             logger.debug(f"[LLM] Sending text prompt (truncated): {log_prompt}")
             messages = [{"role": "user", "content": prompt}]
             response = self.client.chat.completions.create(
                 model=self.LLM_model_name,
                 messages=messages,
                 max_tokens=1024, # Adjust as needed
             )
             logger.debug("[LLM] Received text response.")

             if response.choices:
                 message = response.choices[0].message
                 if message.content:
                     return message.content
                 else:
                     # Handle cases like function calls if they unexpectedly occur or content filter
                     finish_reason = response.choices[0].finish_reason
                     logger.warning(f"[LLM] Text generation returned no content. Finish reason: {finish_reason}. Response: {response.model_dump_json(indent=2)}")
                     if finish_reason == 'content_filter':
                         return "Error: [LLM] Content generation blocked due to content filter."
                     return "Error: [LLM] Empty response from LLM."
             else:
                 logger.warning(f"[LLM] Text generation returned no choices. Response: {response.model_dump_json(indent=2)}")
                 return "Error: [LLM] No choices returned from LLM."

         except openai.APIError as e:
             # Handle API error here, e.g. retry or log
             logger.error(f"[LLM] OpenAI API returned an API Error: {e}", exc_info=True)
             return f"Error: [LLM] API Error - {type(e).__name__}: {e}"
         except openai.AuthenticationError as e:
             logger.error(f"[LLM] OpenAI API authentication error: {e}", exc_info=True)
             return f"Error: [LLM] Authentication Error - {e}"
         except openai.RateLimitError as e:
             logger.error(f"[LLM] OpenAI API request exceeded rate limit: {e}", exc_info=True)
             # Note: Our simple time.sleep might not be enough for LLM's complex limits
             return f"Error: [LLM] Rate limit exceeded - {e}"
         except Exception as e:
             logger.error(f"Error during LLM text generation: {e}", exc_info=True)
             return f"Error: [LLM] Failed to communicate with API - {type(e).__name__}: {e}"

     def _generate_multimodal_LLM(self, prompt: str, image_bytes: bytes) -> str:
        if not self.LLM_vision_model_name:
             logger.error("[LLM] LLM vision model name not configured.")
             return "Error: [LLM] Vision model not configured."

        base64_url = _image_bytes_to_base64_url(image_bytes)
        if not base64_url:
            return "Error: [LLM] Failed to convert image to base64."

        try:
            log_prompt = prompt[:200] + ('...' if len(prompt) > 200 else '')
            logger.debug(f"[LLM] Sending multimodal prompt (truncated): {log_prompt} with image.")

            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": base64_url}},
                    ],
                }
            ]

            response = self.client.chat.completions.create(
                model=self.LLM_vision_model_name, # Use the vision model deployment
                messages=messages,
                max_tokens=1024, # Adjust as needed
            )
            logger.debug("[LLM] Received multimodal response.")

            # Parsing logic similar to text generation
            if response.choices:
                message = response.choices[0].message
                if message.content:
                    return message.content
                else:
                    finish_reason = response.choices[0].finish_reason
                    logger.warning(f"[LLM] Multimodal generation returned no content. Finish reason: {finish_reason}. Response: {response.model_dump_json(indent=2)}")
                    if finish_reason == 'content_filter':
                        return "Error: [LLM] Content generation blocked due to content filter."
                    return "Error: [LLM] Empty multimodal response from LLM."
            else:
                logger.warning(f"[LLM] Multimodal generation returned no choices. Response: {response.model_dump_json(indent=2)}")
                return "Error: [LLM] No choices returned from Vision LLM."

        except openai.APIError as e:
             logger.error(f"[LLM] OpenAI Vision API returned an API Error: {e}", exc_info=True)
             return f"Error: [LLM] Vision API Error - {type(e).__name__}: {e}"
        # Add other specific openai exceptions as needed (AuthenticationError, RateLimitError, etc.)
        except Exception as e:
            logger.error(f"Error during LLM multimodal generation: {e}", exc_info=True)
            return f"Error: [LLM] Failed to communicate with Vision API - {type(e).__name__}: {e}"


     def _generate_json_LLM(self, Schema_Class: Type[BaseModel], prompt: str, image_bytes: Optional[bytes] = None) -> Union[Dict[str, Any], str]:
         if not issubclass(Schema_Class, BaseModel):
              logger.error(f"[LLM] Schema_Class must be a Pydantic BaseModel for LLM JSON generation.")
              return "Error: [LLM] Invalid schema type provided."

         current_model = self.LLM_model_name
         messages: List[Dict[str, Any]] = [{"role": "user", "content": []}] # Initialize user content as list

         # Prepare content (text and optional image)
         text_content = {"type": "text", "text": prompt}
         messages[0]["content"].append(text_content) # type: ignore

         log_msg_suffix = ""
         if image_bytes is not None:
             if not self.LLM_vision_model_name:
                  logger.error("[LLM] LLM vision model name not configured for multimodal JSON.")
                  return "Error: [LLM] Vision model not configured for multimodal JSON."
             current_model = self.LLM_vision_model_name # Use vision model if image is present

             base64_url = _image_bytes_to_base64_url(image_bytes)
             if not base64_url:
                 return "Error: [LLM] Failed to convert image to base64 for JSON."
             image_content = {"type": "image_url", "image_url": {"url": base64_url}}
             messages[0]["content"].append(image_content) # type: ignore
             log_msg_suffix = " with image"


         # Prepare the tool based on the Pydantic schema
         try:
             tool_def = openai.pydantic_function_tool(Schema_Class)
             tools = [tool_def]
             # Tool choice can force the model to use the function, or let it decide.
             # Forcing it: tool_choice = {"type": "function", "function": {"name": Schema_Class.__name__}}
             # Letting it decide (often better unless you *know* it must be called): tool_choice = "auto"
             # Let's explicitly request the tool for structured output
             tool_choice = {"type": "function", "function": {"name": tool_def['function']['name']}}

         except Exception as tool_err:
             logger.error(f"[LLM] Failed to create tool definition from schema {Schema_Class.__name__}: {tool_err}", exc_info=True)
             return f"Error: [LLM] Failed to create tool definition - {tool_err}"


         try:
             log_prompt = prompt[:200] + ('...' if len(prompt) > 200 else '')
             logger.debug(f"[LLM] Sending JSON prompt (truncated): {log_prompt}{log_msg_suffix} with schema {Schema_Class.__name__}")

             # Add a system prompt to guide the model (optional but helpful)
             system_message = {"role": "system", "content": f"You are a helpful assistant. Use the provided '{Schema_Class.__name__}' tool to structure your response based on the user's request."}
             final_messages = [system_message] + messages

             response = self.client.chat.completions.create(
                 model=current_model, # Use vision model if image included
                 messages=final_messages,
                 tools=tools,
                 tool_choice=tool_choice, # Request the specific tool
                 max_tokens=2048, # Adjust as needed
             )
             logger.debug("[LLM] Received JSON response structure.")

             if response.choices:
                 message = response.choices[0].message
                 finish_reason = response.choices[0].finish_reason

                 if message.tool_calls:
                     if len(message.tool_calls) > 1:
                          logger.warning(f"[LLM] Multiple tool calls received, using the first one for schema {Schema_Class.__name__}")

                     tool_call = message.tool_calls[0]
                     if tool_call.type == 'function' and tool_call.function.name == tool_def['function']['name']:
                         function_args_str = tool_call.function.arguments
                         try:
                             # Parse the arguments string into a dictionary
                             parsed_args = json.loads(function_args_str)
                             # Validate and potentially instantiate the Pydantic model
                             model_instance = Schema_Class.model_validate(parsed_args)
                             return model_instance # Return as dict
                         #     print(parsed_args)
                         #     return parsed_args # Return the parsed dict directly
                         except json.JSONDecodeError as json_err:
                             logger.error(f"[LLM] Failed to parse JSON arguments from tool call: {json_err}. Arguments: '{function_args_str}'")
                             return f"Error: [LLM] Failed to parse JSON arguments - {json_err}"
                         except Exception as val_err: # Catch Pydantic validation errors if model_validate is used
                             logger.error(f"[LLM] JSON arguments failed validation for schema {Schema_Class.__name__}: {val_err}. Arguments: {function_args_str}")
                             return f"Error: [LLM] JSON arguments failed validation - {val_err}"
                     else:
                         logger.warning(f"[LLM] Expected function tool call for {Schema_Class.__name__} but got type '{tool_call.type}' or name '{tool_call.function.name}'.")
                         return f"Error: [LLM] Unexpected tool call type/name received."

                 elif finish_reason == 'tool_calls':
                      # This might happen if the model intended to call but failed, or structure is odd
                      logger.warning(f"[LLM] Finish reason is 'tool_calls' but no tool_calls found in message. Response: {response.model_dump_json(indent=2)}")
                      return "Error: [LLM] Model indicated tool use but none found."
                 elif finish_reason == 'content_filter':
                      logger.warning(f"[LLM] JSON generation blocked due to content filter.")
                      return "Error: [LLM] Content generation blocked due to content filter."
                 else:
                      # Model didn't use the tool
                      logger.warning(f"[LLM] Model did not use the requested JSON tool {Schema_Class.__name__}. Finish reason: {finish_reason}. Content: {message.content}")
                      # You might return the text content or an error depending on requirements
                      # return message.content or "Error: [LLM] Model generated text instead of using the JSON tool."
                      return f"Error: [LLM] Model did not use the JSON tool. Finish Reason: {finish_reason}."

             else:
                 logger.warning(f"[LLM] JSON generation returned no choices. Response: {response.model_dump_json(indent=2)}")
                 return "Error: [LLM] No choices returned from LLM for JSON request."

         except openai.APIError as e:
             logger.error(f"[LLM] OpenAI API returned an API Error during JSON generation: {e}", exc_info=True)
             return f"Error: [LLM] API Error (JSON) - {type(e).__name__}: {e}"
         # Add other specific openai exceptions (AuthenticationError, RateLimitError, etc.)
         except Exception as e:
             logger.error(f"Error during LLM JSON generation: {e}", exc_info=True)
             return f"Error: [LLM] Failed to communicate with API for JSON - {type(e).__name__}: {e}"


     # --- Public API Methods ---

     def generate_text(self, prompt: str) -> str:
          """Generates text using the configured LLM provider, respecting rate limits."""
          self._wait_for_rate_limit() # Wait before making the API call
          if self.provider == 'gemini':
               return self._generate_text_gemini(prompt)
          elif self.provider == 'llm':
               return self._generate_text_LLM(prompt)
          else:
               # Should not happen due to __init__ check, but as a fallback
               logger.error(f"generate_text called with unsupported provider '{self.provider}'")
               return f"Error: Unsupported provider '{self.provider}'"


     def generate_multimodal(self, prompt: str, image_bytes: bytes) -> str:
          """Generates text based on a prompt and an image, respecting rate limits."""
          self._wait_for_rate_limit() # Wait before making the API call
          if self.provider == 'gemini':
               return self._generate_multimodal_gemini(prompt, image_bytes)
          elif self.provider == 'llm':
               return self._generate_multimodal_LLM(prompt, image_bytes)
          else:
               logger.error(f"generate_multimodal called with unsupported provider '{self.provider}'")
               return f"Error: Unsupported provider '{self.provider}'"


     def generate_json(self, Schema_Class: Type, prompt: str, image_bytes: Optional[bytes] = None) -> Union[Dict[str, Any], str]:
          """
          Generates structured JSON output based on a prompt, an optional image,
          and a defined schema, respecting rate limits.

          For LLM, Schema_Class must be a Pydantic BaseModel.
          For Gemini, Schema_Class should be a Pydantic BaseModel or compatible type.

          Returns:
              A dictionary representing the parsed JSON on success, or an error string.
          """
          self._wait_for_rate_limit()
          if self.provider == 'gemini':
               # Assuming Gemini needs a type directly (like Pydantic or dataclass)
               return self._generate_json_gemini(Schema_Class, prompt, image_bytes)
          elif self.provider == 'llm':
               if not OPENAI_SDK or not issubclass(Schema_Class, BaseModel):
                    err_msg = "Error: [LLM] Pydantic BaseModel schema required for LLM JSON generation."
                    if not OPENAI_SDK:
                         err_msg = "Error: [LLM] 'openai' and 'pydantic' libraries required for LLM JSON generation."
                    logger.error(err_msg)
                    return err_msg
               return self._generate_json_LLM(Schema_Class, prompt, image_bytes) # Pass the Pydantic class
          else:
               logger.error(f"generate_json called with unsupported provider '{self.provider}'")
               return f"Error: Unsupported provider '{self.provider}'"