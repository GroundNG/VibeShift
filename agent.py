# agent.py
import json
import logging
import time
import re
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
from typing import Dict, Any, Optional, List, Tuple, Union, Literal
import random
import os
import threading # For timer
from datetime import datetime
from pydantic import BaseModel, Field, validator

# Use relative imports within the package
from browser_controller import BrowserController
from llm_client import GeminiClient
from vision_processor import VisionProcessor
from task_manager import TaskManager
from dom.views import DOMState, DOMElementNode, SelectorMap # Import DOM types

# Configure logger
logger = logging.getLogger(__name__)

# --- Recorder Settings ---
INTERACTIVE_TIMEOUT_SECS = 5 # Time for user to override AI suggestion
DEFAULT_WAIT_AFTER_ACTION = 0.5 # Default small wait added after recorded actions
# --- End Recorder Settings ---

class PlanSubtasksSchema(BaseModel):
    """Schema for the planned subtasks list."""
    planned_steps: List[str] = Field(..., description="List of planned test step descriptions as strings.")

class LLMVerificationParamsSchema(BaseModel):
    """Schema for parameters within a successful verification."""
    expected_text: Optional[str] = Field(None, description="Expected text for equals/contains assertions.")
    attribute_name: Optional[str] = Field(None, description="Attribute name for attribute_equals assertion.")
    expected_value: Optional[str] = Field(None, description="Expected value for attribute_equals assertion.")
    expected_count: Optional[int] = Field(None, description="Expected count for element_count assertion.")

class LLMVerificationSchema(BaseModel):
    """Schema for the result of an LLM verification step."""
    verified: bool = Field(..., description="True if the condition is met, False otherwise.")
    assertion_type: Optional[Literal[
        'assert_text_equals',
        'assert_text_contains',
        'assert_visible',
        'assert_hidden',
        'assert_attribute_equals',
        'assert_element_count',
        'assert_checked',        
        'assert_not_checked'   
    ]] = Field(None, description="Required if verified=true. Type of assertion suggested, reflecting the *actual observed state*.")
    element_index: Optional[int] = Field(None, description="Index of the *interactive* element [index] from context that might *also* relate to the verification (e.g., the button just clicked), if applicable. Set to null if verification relies solely on a static element or non-indexed element.")
    verification_selector: Optional[str] = Field(None, description="Required if verified=true. CSS selector of the element (static or interactive) that *directly confirms* the verification condition based on the observed page state.")
    parameters: Optional[LLMVerificationParamsSchema] = Field(default_factory=dict, description="Parameters for the assertion based on the *actual observed state*. Required if assertion type needs params (e.g., assert_text_equals).")
    reasoning: str = Field(..., description="Explanation for the verification result, explaining how the intent is met or why it failed. If verified=true, justify the chosen selector and parameters.")
    
    
class ReplanSchema(BaseModel):
    """Schema for recovery steps or abort action during re-planning."""
    recovery_steps: Optional[List[str]] = Field(None, description="List of recovery step descriptions (1-3 steps), if recovery is possible.")
    action: Optional[Literal["abort"]] = Field(None, description="Set to 'abort' if recovery is not possible/safe.")
    reasoning: Optional[str] = Field(None, description="Reasoning, especially required if action is 'abort'.")

class RecorderSuggestionParamsSchema(BaseModel):
    """Schema for parameters within a recorder action suggestion."""
    index: Optional[int] = Field(None, description="Index of the target element from context (required for click/type).")
    text: Optional[str] = Field(None, description="Text to type (required for type action).")

class RecorderSuggestionSchema(BaseModel):
    """Schema for the AI's suggestion for a click/type action during recording."""
    action: Literal["click", "type", "check", "uncheck", "action_not_applicable", "suggestion_failed"] = Field(..., description="The suggested browser action or status.")
    parameters: RecorderSuggestionParamsSchema = Field(default_factory=dict, description="Parameters for the action (index, text).")
    reasoning: str = Field(..., description="Explanation for the suggestion.")

class AssertionTargetIndexSchema(BaseModel):
    """Schema for identifying the target element index for a manual assertion."""
    index: Optional[int] = Field(None, description="Index of the most relevant element from context, or null if none found/identifiable.")
    reasoning: Optional[str] = Field(None, description="Reasoning, especially if index is null.")
    

class WebAgent:
    """
    Orchestrates AI-assisted web test recording, generating reproducible test scripts.
    Can also function in a (now legacy) execution mode.
    """

    def __init__(self,
                 gemini_client: GeminiClient,
                 headless: bool = True, # Note: Recorder mode forces non-headless
                 max_iterations: int = 50, # Max planned steps to process in recorder
                 max_history_length: int = 10,
                 max_retries_per_subtask: int = 1, # Retries for *AI suggestion* or failed *execution* during recording
                 max_extracted_data_history: int = 7, # Less relevant for recorder? Keep for now.
                 is_recorder_mode: bool = False,
                 automated_mode: bool = False): 

        self.gemini_client = gemini_client
        self.is_recorder_mode = is_recorder_mode
        # Determine effective headless: Recorder forces non-headless unless automated
        effective_headless = headless
        if self.is_recorder_mode and not automated_mode:
            effective_headless = False # Interactive recording needs visible browser
            if headless:
                logger.warning("Interactive Recorder mode initiated, but headless=True was requested. Forcing headless=False.")
        elif automated_mode and not headless:
            logger.info("Automated mode running with visible browser (headless=False).")

        self.browser_controller = BrowserController(headless=effective_headless)
        self.vision_processor = VisionProcessor(gemini_client)
        # TaskManager manages the *planned* steps generated by LLM initially
        self.task_manager = TaskManager(max_retries_per_subtask=max_retries_per_subtask)
        self.history: List[Dict[str, Any]] = []
        self.extracted_data_history: List[Dict[str, Any]] = [] # Keep for potential context, but less critical now
        self.max_iterations = max_iterations # Limit for planned steps processing
        self.max_history_length = max_history_length
        self.max_extracted_data_history = max_extracted_data_history
        self.output_file_path: Optional[str] = None # Path for the recorded JSON
        self.feature_description: Optional[str] = None
        self._latest_dom_state: Optional[DOMState] = None
        self._consecutive_suggestion_failures = 0 # Track failures for the *same* step index
        self._last_failed_step_index = -1 # Track which step index had the last failure
        # --- Recorder Specific State ---
        self.recorded_steps: List[Dict[str, Any]] = []
        self._current_step_id = 1 # Counter for recorded steps
        self._user_abort_recording = False
        # --- End Recorder Specific State ---
        self.automated_mode = automated_mode

        # Log effective mode
        mode_name = "Recorder" if self.is_recorder_mode else "Execution (Legacy)"
        automation_status = "Automated" if self.automated_mode else "Interactive"
        logger.info(f"WebAgent ({mode_name} Mode / {automation_status}) initialized (headless={effective_headless}, max_planned_steps={max_iterations}, max_hist={max_history_length}, max_retries={max_retries_per_subtask}).")


    def _add_to_history(self, entry_type: str, data: Any):
        """Adds an entry to the agent's history, maintaining max length."""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_data_str = "..."
        try:
            # Basic sanitization (same as before)
            if isinstance(data, dict):
                log_data = {k: (str(v)[:200] + '...' if len(str(v)) > 200 else v)
                             for k, v in data.items()}
            elif isinstance(data, (str, bytes)):
                 log_data = str(data[:297]) + "..." if len(data) > 300 else str(data)
            else:
                 log_data = data
            log_data_str = str(log_data)
            if len(log_data_str) > 300: log_data_str = log_data_str[:297]+"..."
        except Exception as e:
            logger.warning(f"Error sanitizing history data: {e}")
            log_data = f"Error processing data: {e}"
            log_data_str = log_data

        entry = {"timestamp": timestamp, "type": entry_type, "data": log_data}
        self.history.append(entry)
        if len(self.history) > self.max_history_length:
            self.history.pop(0)
        logger.debug(f"[HISTORY] Add: {entry_type} - {log_data_str}")

    def _get_history_summary(self) -> str:
        """Provides a concise summary of the recent history for the LLM."""
        # (Implementation remains the same as before)
        if not self.history: return "No history yet."
        summary = "Recent History (Oldest First):\n"
        for entry in self.history:
            entry_data_str = str(entry['data'])
            if len(entry_data_str) > 300: entry_data_str = entry_data_str[:297] + "..."
            summary += f"- [{entry['type']}] {entry_data_str}\n"
        return summary.strip()

    def _clean_llm_response_to_json(self, llm_output: str) -> Optional[Dict[str, Any]]:
        """Attempts to extract and parse JSON from the LLM's output."""
        # (Implementation remains the same as before)
        logger.debug(f"[LLM PARSE] Attempting to parse LLM response (length: {len(llm_output)}).")
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", llm_output, re.DOTALL | re.IGNORECASE)
        if match:
            json_str = match.group(1).strip()
            logger.debug(f"[LLM PARSE] Extracted JSON from markdown block.")
        else:
            start_index = llm_output.find('{')
            end_index = llm_output.rfind('}')
            if start_index != -1 and end_index != -1 and end_index > start_index:
                json_str = llm_output[start_index : end_index + 1].strip()
                logger.debug(f"[LLM PARSE] Attempting to parse extracted JSON between {{ and }}.")
            else:
                 logger.warning("[LLM PARSE] Could not find JSON structure in LLM output.")
                 self._add_to_history("LLM Parse Error", {"reason": "No JSON structure found", "raw_output_snippet": llm_output[:200]})
                 return None

        # Pre-processing (same as before)
        try:
            def escape_quotes_replacer(match):
                key_part, colon_part, open_quote, value, close_quote = match.groups()
                escaped_value = re.sub(r'(?<!\\)"', r'\\"', value)
                return f'{key_part}{colon_part}{open_quote}{escaped_value}{close_quote}'
            keys_to_escape = ["selector", "text", "reasoning", "url", "result", "answer", "reason", "file_path", "expected_text", "attribute_name", "expected_value"]
            pattern_str = r'(\"(?:' + '|'.join(keys_to_escape) + r')\")(\s*:\s*)(\")(.*?)(\")'
            pattern = re.compile(pattern_str, re.DOTALL)
            json_str = pattern.sub(escape_quotes_replacer, json_str)
            json_str = json_str.replace('\\\\n', '\\n').replace('\\n', '\n')
            json_str = json_str.replace('\\\\"', '\\"')
            json_str = json_str.replace('\\\\t', '\\t')
            json_str = re.sub(r',\s*([\}\]])', r'\1', json_str)
        except Exception as clean_e:
             logger.warning(f"[LLM PARSE] Error during pre-parsing cleaning: {clean_e}")

        # Attempt Parsing (check for 'action' primarily, parameters might be optional for some recorder actions)
        try:
            parsed_json = json.loads(json_str)
            if isinstance(parsed_json, dict) and "action" in parsed_json:
                # Parameters might not always be present (e.g., simple scroll)
                if "parameters" not in parsed_json:
                     parsed_json["parameters"] = {} # Ensure parameters key exists
                logger.debug(f"[LLM PARSE] Successfully parsed action JSON: {parsed_json}")
                return parsed_json
            else:
                 logger.warning(f"[LLM PARSE] Parsed JSON missing 'action' key or is not a dict: {parsed_json}")
                 self._add_to_history("LLM Parse Error", {"reason": "Missing 'action' key", "parsed_json": parsed_json, "cleaned_json_string": json_str[:200]})
                 return None
        except json.JSONDecodeError as e:
            logger.error(f"[LLM PARSE] Failed to decode JSON from LLM output: {e}")
            logger.error(f"[LLM PARSE] Faulty JSON string snippet (around pos {e.pos}): {json_str[max(0, e.pos-50):e.pos+50]}")
            self._add_to_history("LLM Parse Error", {"reason": f"JSONDecodeError: {e}", "error_pos": e.pos, "json_string_snippet": json_str[max(0, e.pos-50):e.pos+50]})
            return None
        except Exception as e:
             logger.error(f"[LLM PARSE] Unexpected error during final JSON parsing: {e}", exc_info=True)
             return None

    def _plan_subtasks(self, feature_description: str):
        """Uses the LLM to break down the feature test into planned steps using generate_json."""
        logger.info(f"Planning test steps for feature: '{feature_description}'")
        self.feature_description = feature_description

        # --- Prompt Construction (Adjusted for generate_json) ---
        prompt = f"""
        You are an AI Test Engineer planning steps for recording. Given the feature description: "{feature_description}"

        Break this down into a sequence of specific browser actions or verification checks.
        Each step should be a single instruction (e.g., "Navigate to...", "Click the 'Submit' button", "Type 'testuser' into username field", "Verify text 'Success' is visible").
        The recorder agent will handle identifying elements and generating selectors based on these descriptions.

        **Key Types of Steps to Plan:**
        1.  **Navigation:** `Navigate to https://example.com/login`
        2.  **Action:** `Click element 'Submit Button'` or `Type 'testuser' into element 'Username Input'` or `Check 'male' radio button or Check 'Agree to terms & conditions'` or `Uncheck the 'subscribe to newsletter' checkbox` (Describe the element clearly)
        3.  **Verification:** Phrase as a check. The recorder will prompt for specifics.
            - `Verify 'Login Successful' message is present`
            - `Verify 'Cart Count' shows 1`
            - `Verify 'Submit' button is disabled`
            - **GOOD:** `Verify login success indicator is visible` (More general)
            - **AVOID:** `Verify text 'Welcome John Doe!' is visible` (Too specific if name changes)
        4.  **Scrolling:** `Scroll down` (if content might be off-screen)

        **CRITICAL:** Focus on the *intent* of each step. Do NOT include specific selectors or indices in the plan. The recorder determines those interactively.

        **Output Format:** Respond with a JSON object conforming to the following structure:
        {{
          "planned_steps": ["Step 1 description", "Step 2 description", ...]
        }}

        Example Test Case: "Test login on example.com with user 'tester' and pass 'pwd123', then verify the welcome message 'Welcome, tester!' is shown."
        Example JSON Output Structure:
        {{
          "planned_steps": [
            "Navigate to https://example.com/login",
            "Type 'tester' into element 'username input field'",
            "Type 'pwd123' into element 'password input field'",
            "Click element 'login button'",
            "Verify 'Welcome, tester!' message is present"
          ]
        }}

        Now, generate the JSON object containing the planned steps for: "{feature_description}"
        """
        # --- End Prompt ---

        logger.debug(f"[TEST PLAN] Sending Planning Prompt (snippet):\n{prompt[:500]}...")
        # <<< START CHANGE >>>
        # Replace text generation and manual parsing with generate_json
        response_obj = self.gemini_client.generate_json(PlanSubtasksSchema, prompt)

        subtasks = None
        raw_response_for_history = "N/A (Used generate_json)"

        if isinstance(response_obj, PlanSubtasksSchema):
            logger.debug(f"[TEST PLAN] LLM JSON response parsed successfully: {response_obj}")
            # Validate the parsed list
            if isinstance(response_obj.planned_steps, list) and all(isinstance(s, str) and s for s in response_obj.planned_steps):
                subtasks = response_obj.planned_steps
            else:
                logger.warning(f"[TEST PLAN] Parsed JSON planned_steps is not a list of non-empty strings: {response_obj.planned_steps}")
                raw_response_for_history = f"Parsed object invalid content: {response_obj}" # Log the invalid object
        elif isinstance(response_obj, str): # Handle error string from generate_json
             logger.error(f"[TEST PLAN] Failed to generate/parse planned steps JSON from LLM: {response_obj}")
             raw_response_for_history = response_obj[:500]+"..."
        else: # Handle unexpected return type
             logger.error(f"[TEST PLAN] Unexpected response type from generate_json: {type(response_obj)}")
             raw_response_for_history = f"Unexpected type: {type(response_obj)}"

        # --- Update Task Manager ---
        if subtasks and len(subtasks) > 0:
            self.task_manager.add_subtasks(subtasks) # TaskManager stores the *planned* steps
            self._add_to_history("Test Plan Created", {"feature": feature_description, "steps": subtasks})
            logger.info(f"Successfully planned {len(subtasks)} test steps.")
            logger.debug(f"[TEST PLAN] Planned Steps: {subtasks}")
        else:
            logger.error("[TEST PLAN] Failed to generate or parse valid planned steps from LLM response.")
            # Use the captured raw_response_for_history which contains error details
            self._add_to_history("Test Plan Failed", {"feature": feature_description, "raw_response": raw_response_for_history})
            raise ValueError("Failed to generate a valid test plan from the feature description.")

    def _get_extracted_data_summary(self) -> str:
        """Provides summary of extracted data (less critical for recorder, but kept for potential context)."""
        # (Implementation remains the same)
        if not self.extracted_data_history: return "No data extracted yet."
        summary = "Recently Extracted Data (Context Only):\n"
        start_index = max(0, len(self.extracted_data_history) - self.max_extracted_data_history)
        for entry in reversed(self.extracted_data_history[start_index:]):
             data_snippet = str(entry.get('data', ''))[:150] + "..." if len(str(entry.get('data', ''))) > 150 else str(entry.get('data', ''))
             step_desc_snippet = entry.get('subtask_desc', 'N/A')[:50] + ('...' if len(entry.get('subtask_desc', 'N/A')) > 50 else '')
             index_info = f"Index:[{entry.get('index', '?')}]"
             selector_info = f" Sel:'{entry.get('selector', '')[:30]}...'" if entry.get('selector') else ""
             summary += f"- Step {entry.get('subtask_index', '?')+1} ('{step_desc_snippet}'): {index_info}{selector_info} Type={entry.get('type')}, Data={data_snippet}\n"
        return summary.strip()

    def _get_llm_verification(self,
                               verification_description: str,
                               current_url: str,
                               dom_context_str: str,
                               screenshot_bytes: Optional[bytes] = None
                               ) -> Optional[Dict[str, Any]]:
        """
        Uses LLM's generate_json (potentially multimodal) to verify if a condition is met.
        Returns a dictionary representation of the result or None on error.
        """

        logger.info(f"Requesting LLM verification (using generate_json) for: '{verification_description}'")

        # --- Prompt Adjustment for generate_json ---
        prompt = f"""
You are an AI Test Verification Assistant. Your task is to determine if a specific condition, **or its clear intent**, is met based on the current web page state.

**Overall Goal:** {self.feature_description}
**Verification Step:** {verification_description}
**Current URL:** {current_url}

**Input Context (Visible Elements with Indices for Interactive ones):**
This section shows visible elements on the page.
- Interactive elements are marked with `[index]` (e.g., `[5]<button>Submit</button>`).
- Static elements crucial for context are marked with `(Static)` (e.g., `<p (Static)>Login Successful!</p>`).
- Some plain static elements may include a hint about their parent, like `(inside: <div id="summary">)`, to help locate them.
```html
{dom_context_str}
```
{f"**Screenshot Analysis:** Please analyze the attached screenshot for visual confirmation or contradiction of the verification step." if screenshot_bytes else "**Note:** No screenshot provided for visual analysis."}

**Your Task:**
1.  Analyze the provided context (DOM, URL, and screenshot if provided).
2.  Determine if the **intent** behind the "Verification Step" is currently TRUE or FALSE.
    *   Example: If the step is "Verify 'Login Complete'", but the page shows "Welcome, User!", the *intent* IS met.
3.  Respond with a JSON object matching the required schema.
    *   Set the `verified` field (boolean).
    *   Provide detailed `reasoning` (string), explaining *how* the intent is met or why it failed.
    *   **If `verified` is TRUE:**
        *   Identify the **single most relevant element** (interactive OR static) in the context that **confirms the successful state**.
        *   **`verification_selector` (Required):** Provide a robust CSS selector. If the element is static and has a parent hint like `(inside: <div id="parent-id">)`, try to incorporate the parent into the selector (e.g., `#parent-id > span.label`). Use attributes (id, data-testid, name, class) or specific text content shown in the context.
        *   **`element_index` (Optional):** If the confirming element is an *interactive* element with an `[index]`, set `element_index` to that index. Otherwise (if confirmed by a static element or non-indexed interactive element), set `element_index` to `null`.
        *   **`assertion_type` (Required):** Determine the most appropriate assertion type based on the **actual observed state** and the verification intent.
            *   Use `assert_checked` if the intent is to verify a checkbox or radio button **is currently selected/checked**.
            *   Use `assert_not_checked` if the intent is to verify it **is NOT selected/checked**.
            *   Use `assert_visible` / `assert_hidden` for visibility states.
            *   Use `assert_text_equals` / `assert_text_contains` for text content.
            *   Use `assert_attribute_equals` ONLY for comparing the *string value* of an attribute (e.g., `class="active"`, `value="Completed"`). **DO NOT use it for boolean attributes like `checked`, `disabled`, `selected`. Use state assertions instead.**
            *   Use `assert_element_count` for counting elements matching a selector.
        *   **`parameters` (Optional):** Provide necessary parameters ONLY if the chosen `assertion_type` requires them (e.g., `assert_text_equals` needs `expected_text`). For `assert_checked`, `assert_not_checked`, `assert_visible`, `assert_hidden`, parameters should generally be empty (`{{}}`) or omitted. Ensure parameters reflect the *actual observed state* (e.g., observed text).
    *   **If `verified` is FALSE:**
        *   `assertion_type`, `element_index`, `verification_selector`, `parameters` should typically be null/omitted.

**JSON Output Structure Examples:**

*Success Case (Static Element Confirms):*
```json
{{
  "verified": true,
  "assertion_type": "assert_text_equals",
  "element_index": null,
  "verification_selector": "p.success-message",
  "parameters": {{ "expected_text": "Congratulations student. You successfully logged in!" }},
  "reasoning": "Verification step asked for 'You logged into...', but the static <p class='success-message' (Static)> element shows the success message 'Congratulations student...', confirming the login intent."
}}
```
*Success Case (Radio Button Checked):*
```json
{{
  "verified": true,
  "assertion_type": "assert_checked",
  "element_index": 9,
  "verification_selector": "input[name='paymentMethod'][value='creditCard']",
  "parameters": {{}},
  "reasoning": "The 'Credit Card' radio button [9] is selected on the page, fulfilling the verification requirement."
}}
```
*Success Case (Checkbox Not Checked):*
```json
{{
  "verified": true,
  "assertion_type": "assert_not_checked",
  "element_index": 11,
  "verification_selector": "input#subscribeNews",
  "parameters": {{}},
  "reasoning": "The 'Subscribe' checkbox [11] is not checked, as required by the verification step."
}}
```
*Success Case (Interactive Element Confirms):*
```json
{{
  "verified": true,
  "assertion_type": "assert_visible",
  "element_index": 8,
  "verification_selector": "button[data-testid='logout-button']",
  "parameters": {{}},
  "reasoning": "Element [8] (logout button) is visible, confirming the user is logged in as per the verification step intent."
}}
```
*Success Case (Attribute on Static Element):*
```json
{{
  "verified": true,
  "assertion_type": "assert_attribute_equals",
  "element_index": null,
  "verification_selector": "div#user-status[data-status='active']",
  "parameters": {{ "attribute_name": "data-status", "expected_value": "active" }},
  "reasoning": "The static <div id='user-status' data-status='active' (Static)> element has the 'data-status' attribute set to 'active', confirming the verification requirement."
}}
```
*Failure Case:*
```json
{{
  "verified": false,
  "assertion_type": null,
  "element_index": null,
  "verification_selector": null,
  "parameters": {{}},
  "reasoning": "Could not find the 'Success' message or any other indication of successful login in the provided context or screenshot."
}}
```

**CRITICAL:** If `verified` is true, the `verification_selector` MUST point to the element that confirms the intent. `assertion_type` and `parameters` MUST reflect the *actual observed state*. Explain any discrepancies between the plan and the observed state in `reasoning`. Respond ONLY with the JSON object matching the schema.

Now, generate the verification JSON for: "{verification_description}"
"""

        # Call generate_json, passing image_bytes if available
        logger.debug("[LLM VERIFY] Sending prompt (and potentially image) to generate_json...")
        response_obj = self.gemini_client.generate_json(
            LLMVerificationSchema,
            prompt,
            image_bytes=screenshot_bytes # Pass the image bytes here
        )

        verification_json = None # Initialize

        if isinstance(response_obj, LLMVerificationSchema):
             logger.debug(f"[LLM VERIFY] Successfully parsed response: {response_obj}")
             verification_dict = response_obj.model_dump(exclude_none=True)
             assertion_type = verification_dict.get("assertion_type")
             params = verification_dict.get("parameters", {})
             needs_params = assertion_type in ['assert_text_equals', 'assert_text_contains', 'assert_attribute_equals', 'assert_element_count']
             no_params_needed = assertion_type in ['assert_checked', 'assert_not_checked', 'assert_visible', 'assert_hidden']
             # --- Post-hoc Validation ---
             is_verified = verification_dict.get("verified")
             if is_verified is None:
                  logger.error("[LLM VERIFY FAILED] Parsed JSON missing required 'verified' field.")
                  self._add_to_history("LLM Verification Error", {"reason": "Missing 'verified' field", "parsed_dict": verification_dict})
                  return None

             if not verification_dict.get("reasoning"):
                  logger.warning(f"[LLM VERIFY] Missing 'reasoning' in response: {verification_dict}")
                  verification_dict["reasoning"] = "No reasoning provided by LLM."

             if is_verified:
                if needs_params and not params:
                     logger.warning(f"[LLM VERIFY WARN] Verified=true and assertion '{assertion_type}' typically needs parameters, but none provided: {verification_dict}")
                     # Don't fail, but log it. Maybe fallback later if needed.
                elif no_params_needed and params:
                     logger.warning(f"[LLM VERIFY WARN] Verified=true and assertion '{assertion_type}' typically needs no parameters, but some provided: {params}. Using empty params.")
                     verification_dict["parameters"] = {}

             verification_json = verification_dict # Assign the validated dictionary

        elif isinstance(response_obj, str): # Handle error string
             logger.error(f"[LLM VERIFY FAILED] LLM returned an error string: {response_obj}")
             self._add_to_history("LLM Verification Failed", {"raw_error_response": response_obj})
             return None
        else: # Handle unexpected type
             logger.error(f"[LLM VERIFY FAILED] Unexpected response type from generate_json: {type(response_obj)}")
             self._add_to_history("LLM Verification Failed", {"response_type": str(type(response_obj))})
             return None


        if verification_json:
            logger.info(f"[LLM VERIFY RESULT] Verified: {verification_json['verified']}, Selector: {verification_json.get('verification_selector')}, Reasoning: {verification_json.get('reasoning', '')[:150]}...")
            self._add_to_history("LLM Verification Result", verification_json)
            return verification_json # Return the dictionary
        else:
             logger.error("[LLM VERIFY FAILED] Reached end without valid verification_json.")
             return None

    def _handle_llm_verification(self, planned_step: Dict[str, Any], verification_result: Dict[str, Any]) -> bool:
        """
        Handles the user interaction and recording after LLM verification.
        Now uses verification_selector as the primary target.
        Returns True if handled (recorded/skipped), False if aborted.
        """
        planned_desc = planned_step["description"]
        is_verified = verification_result['verified']
        reasoning = verification_result.get('reasoning', 'N/A')
        step_handled = False # Flag to track if a decision was made

        # --- Automated Mode ---
        if self.automated_mode:
            logger.info(f"[Auto Mode] Handling LLM Verification for: '{planned_desc}'")
            logger.info(f"[Auto Mode] AI Result: {'PASSED' if is_verified else 'FAILED'}. Reasoning: {reasoning[:150]}...")

            if is_verified:
                final_selector = verification_result.get("verification_selector")
                assertion_type = verification_result.get("assertion_type")
                parameters = verification_result.get("parameters", {})

                if not final_selector or not assertion_type:
                    logger.error("[Auto Mode] AI verification PASSED but missing required selector/assertion_type. Marking step as failed.")
                    self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "failed", result="Failed (Inconsistent AI verification data: missing selector/type)")
                    return True # Skip

                # Record the verified assertion automatically
                logger.info(f"[Auto Mode] Recording verified assertion: {assertion_type} on {final_selector}")
                record = {
                    "step_id": self._current_step_id,
                    "action": assertion_type,
                    "description": planned_desc,
                    "parameters": parameters,
                    "selector": final_selector,
                    "wait_after_secs": 0
                }
                self.recorded_steps.append(record)
                self._current_step_id += 1
                logger.info(f"Step {record['step_id']} recorded (AI Verified - Automated)")
                self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "done", result=f"Recorded AI-verified assertion (automated) as step {record['step_id']}")
                step_handled = True

            else: # Verification failed according to LLM
                # --- Verification FAILED according to LLM ---
                logger.warning(f"[Auto Mode] AI verification FAILED for '{planned_desc}'. Recording failure.")
                # 1. Record a "failed verification" step
                failed_record = {
                    "step_id": self._current_step_id,
                    "action": "assert_failed_verification", # Use a specific action type
                    "description": planned_desc,
                    "parameters": {
                        "reasoning": reasoning # Include AI's reason for failure
                    },
                    "selector": None, # No specific selector applies to the failure itself
                    "wait_after_secs": 0
                }
                self.recorded_steps.append(failed_record)
                self._current_step_id += 1
                logger.info(f"Step {failed_record['step_id']} recorded (AI Verification FAILED - Automated)")
                
                # 2. Mark the planned step in TaskManager as 'failed'
                #    Use force_update=True if necessary, depending on TaskManager logic
                self.task_manager.update_subtask_status(
                    self.task_manager.current_subtask_index,
                    "failed", # Mark as failed
                    error=f"AI verification failed: {reasoning}",
                    force_update=True # Ensure status changes even if retries were possible
                )
                step_handled = True # The step *was* handled by recording the failure

            self.browser_controller.clear_highlights() # Clear highlights even in auto mode
            return True # Indicate handled (recorded success OR recorded failure), loop continues but TaskManager reflects state

        # --- Interactive Mode ---
        else:
            print("\n" + "="*60)
            print(f"Planned Step: {planned_desc}")
            print(f"AI Verification Result: {'PASSED' if verification_result['verified'] else 'FAILED'}")
            print(f"AI Reasoning: {verification_result['reasoning']}")
            print("="*60)

            final_selector = None # Initialize

            # If verification passed, use the verification_selector and try to highlight
            if verification_result["verified"]:
                # --- Get Key Info from Result ---
                final_selector = verification_result.get("verification_selector") # Primary selector
                interactive_index = verification_result.get("element_index") # Optional interactive index
                assertion_type = verification_result.get("assertion_type")
                parameters = verification_result.get("parameters", {})

                if not final_selector: # Should have been caught by validation in _get_llm_verification
                    logger.error("Internal Error: Verified=true but verification_selector is missing!")
                    print("Error: AI verification data is inconsistent (missing selector). Falling back to manual.")
                    return self._handle_assertion_recording(planned_step) # Fallback

                # --- Display Verification Basis and Highlight ---
                self.browser_controller.clear_highlights()
                verification_basis_msg = ""
                highlight_color = "#0000FF" # Blue default

                if interactive_index is not None:
                    # Verification likely confirmed by an interactive element
                    verification_basis_msg = f"AI based verification on Interactive Element [Index: {interactive_index}]"
                    # Try highlighting the interactive element using its map selector first
                    target_node = None
                    if self._latest_dom_state and self._latest_dom_state.selector_map:
                        target_node = self._latest_dom_state.selector_map.get(interactive_index)
                    if target_node and target_node.css_selector:
                        try:
                            self.browser_controller.highlight_element(target_node.css_selector, interactive_index, color=highlight_color, text="Verify Target (Interactive)")
                            print(f"{verification_basis_msg} using selector: `{target_node.css_selector}`")
                        except Exception as hl_err:
                            logger.warning(f"Could not highlight interactive index {interactive_index} ({target_node.css_selector}): {hl_err}. Highlighting verification_selector instead.")
                            # Fallback to highlighting verification_selector
                            try:
                                self.browser_controller.highlight_element(final_selector, 0, color="#FFA500", text="Verify Target (Selector)") # Orange for fallback
                                print(f"{verification_basis_msg}. Using primary verification selector: `{final_selector}`")
                            except Exception as hl_err2:
                                logger.error(f"Could not highlight verification_selector either ({final_selector}): {hl_err2}")
                                print(f"{verification_basis_msg}. Failed to highlight element. Using selector: `{final_selector}`")

                    else: # Interactive index provided but node/selector not found in map
                        logger.warning(f"LLM provided interactive index {interactive_index}, but node/selector not found in map. Relying on verification_selector.")
                        verification_basis_msg = f"AI based verification on element (interactive index {interactive_index} not found)"
                        # Highlight verification_selector
                        try:
                            self.browser_controller.highlight_element(final_selector, 0, color="#FFA500", text="Verify Target (Selector)")
                            print(f"{verification_basis_msg}. Using primary verification selector: `{final_selector}`")
                        except Exception as hl_err:
                            logger.error(f"Could not highlight verification_selector ({final_selector}): {hl_err}")
                            print(f"{verification_basis_msg}. Failed to highlight element. Using selector: `{final_selector}`")

                else:
                    # Verification based on a static element or non-indexed interactive element
                    verification_basis_msg = "AI based verification on Static/Non-Indexed Element"
                    highlight_color = "#008000" # Green for static/direct selector
                    try:
                        # Highlight using the verification_selector directly
                        self.browser_controller.highlight_element(final_selector, 0, color=highlight_color, text="Verify Target (Static/Direct)")
                        print(f"{verification_basis_msg} using selector: `{final_selector}`")
                    except Exception as hl_err:
                        logger.error(f"Could not highlight static/direct verification_selector '{final_selector}': {hl_err}")
                        print(f"{verification_basis_msg}. Failed to highlight element. Using selector: `{final_selector}`")

                # Display Assertion Details
                print(f"  Suggested Assertion: `{assertion_type}`")
                print(f"  Parameters: `{parameters}`")

                # --- User Confirmation ---
                print("\nConfirm AI Verification:")
                print(f"  [Enter/Y] Record assertion: {assertion_type} on '{final_selector}' with params {parameters}")
                print(f"  [M] Define Assertion Manually (override AI)")
                print(f"  [S] Skip this verification step")
                print(f"  [A] Abort recording")
                user_choice = input("Your choice? > ").strip().lower()

                if user_choice == '' or user_choice == 'y':
                    print(f"Recording verified assertion: {assertion_type} on {final_selector}")
                    # --- Record the Assertion Step ---
                    record = {
                        "step_id": self._current_step_id,
                        "action": assertion_type,
                        "description": planned_desc, # Use original planned description
                        "parameters": parameters, # Use parameters from LLM verification
                        "selector": final_selector, # Use the primary verification selector
                        "wait_after_secs": 0
                    }
                    self.recorded_steps.append(record)
                    self._current_step_id += 1
                    logger.info(f"Step {record['step_id']} recorded (AI Verified): {assertion_type} on {final_selector}")
                    self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "done", result=f"Recorded AI-verified assertion as step {record['step_id']}")
                    return True # Success

                elif user_choice == 'm':
                    print("Switching to manual assertion definition...")
                    self.browser_controller.clear_highlights() # Clear AI highlight
                    return self._handle_assertion_recording(planned_step) # Fallback to original manual handler

                elif user_choice == 's':
                    print("Skipping verification step.")
                    self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "skipped", result="User skipped AI verification")
                    return True # Skip

                elif user_choice == 'a':
                    print("Aborting recording.")
                    self._user_abort_recording = True
                    return False # Abort

                else:
                    print("Invalid choice. Skipping verification step.")
                    self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "skipped", result="Invalid choice on AI verification")
                    return True # Skip

            else: # Verification Failed according to LLM
                # --- Logic for failed verification remains the same ---
                print("\nAI could not verify the exact condition.")
                reasoning_snippet = verification_result.get('reasoning', '')[:200] + "..."
                print(f"  AI Reasoning: {reasoning_snippet}")

                suggests_alternative = False
                # (Keep the existing logic for checking reasoning for alternatives)
                if "congratulations" in reasoning_snippet.lower() or \
                "successfully logged in" in reasoning_snippet.lower() or \
                "success message" in reasoning_snippet.lower():
                    suggests_alternative = True
                    print("\n  NOTE: The AI reasoning suggests success *might* have occurred differently.")
                    print("  You might want to define the assertion manually based on what you see.")

                print("\nChoose an action:")
                if suggests_alternative:
                    print("  [M] Define Assertion Manually (Recommended to check actual success state)")
                else:
                    print("  [M] Define Assertion Manually (if you believe AI is wrong)")
                print("  [S] Skip this verification step")
                print("  [A] Abort recording")
                user_choice = input("Your choice? > ").strip().lower()

                if user_choice == 'm':
                    print("Switching to manual assertion definition...")
                    self.browser_controller.clear_highlights()
                    return self._handle_assertion_recording(planned_step) # Allow manual definition

                elif user_choice == 's':
                    print("Skipping verification step (AI check failed, user confirmed skip).")
                    self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "skipped", result="Skipped step (AI verification failed, user chose skip)")
                    return True # Skip

                elif user_choice == 'a':
                    print("Aborting recording.")
                    self._user_abort_recording = True
                    return False # Abort
                else:
                    print("Invalid choice. Skipping verification step.")
                    self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "skipped", result="Invalid choice after failed AI verification")
                    return True # Skip

    def _trigger_re_planning(self, current_planned_task: Dict[str, Any], reason: str) -> bool:
        """
        Attempts to get recovery steps from the LLM (using generate_json, potentially multimodal)
        when an unexpected state is detected.
        Returns True if recovery steps were inserted, False otherwise (or if abort requested).
        """
        logger.warning(f"Triggering re-planning due to: {reason}")
        self._add_to_history("Re-planning Triggered", {"reason": reason, "failed_step_desc": current_planned_task['description']})

        if self.automated_mode:
             print = lambda *args, **kwargs: logger.info(f"[Auto Mode Replanning] {' '.join(map(str, args))}") # Redirect print
             input = lambda *args, **kwargs: 'y' # Default to accepting suggestions in auto mode
        else:
            print("\n" + "*"*60)
            print("! Unexpected State Detected !")
            print(f"Reason: {reason}")
            print(f"Original Goal: {self.feature_description}")
            print(f"Attempting Step: {current_planned_task['description']}")
            print("Asking AI for recovery suggestions...")
            print("*"*60)

        # --- Gather Context for Re-planning ---
        current_url = "Error getting URL"
        dom_context_str = "Error getting DOM"
        screenshot_bytes = None # Initialize
        try:
            current_url = self.browser_controller.get_current_url()
            if self._latest_dom_state and self._latest_dom_state.element_tree:
                 dom_context_str = self._latest_dom_state.element_tree.generate_llm_context_string(context_purpose='verification')
            screenshot_bytes = self.browser_controller.take_screenshot()
        except Exception as e:
             logger.error(f"Error gathering context for re-planning: {e}")

        original_plan_str = "\n".join([f"- {t['description']}" for t in self.task_manager.subtasks])
        history_summary = self._get_history_summary()
        last_done_step_desc = "N/A (Start of test)"
        for i in range(current_planned_task['index'] -1, -1, -1):
             if self.task_manager.subtasks[i]['status'] == 'done':
                   last_done_step_desc = self.task_manager.subtasks[i]['description']
                   break

        # --- Construct Re-planning Prompt (Adjusted for generate_json with image) ---
        prompt = f"""
You are an AI Test Recorder Assistant helping recover from an unexpected state during test recording.

**Overall Goal:** {self.feature_description}
**Original Planned Steps:**
{original_plan_str}

**Current Situation:**
- Last successfully completed planned step: '{last_done_step_desc}'
- Currently trying to execute planned step: '{current_planned_task['description']}' (Attempt {current_planned_task['attempts']})
- Encountered unexpected state/error: {reason}
- Current URL: {current_url}

**Current Page Context (Visible Interactive Elements with Indices):**
```html
{dom_context_str}
```
# <<< START CHANGE >>>
{f"**Screenshot Analysis:** Please analyze the attached screenshot to understand the current visual state and identify elements relevant for recovery." if screenshot_bytes else "**Note:** No screenshot provided for visual analysis."}
# <<< END CHANGE >>>

**Your Task:**
Analyze the current situation, context (DOM, URL, screenshot if provided), and the overall goal.
Generate a JSON object matching the required schema.
- **If recovery is possible:** Provide a **short sequence (1-3 steps)** of recovery actions in the `recovery_steps` field (list of strings). These steps should aim to get back on track towards the original goal OR correctly perform the intended action of the failed step ('{current_planned_task['description']}') in the *current* context. Focus ONLY on the immediate recovery. Example: `["Click element 'Close Popup Button'", "Verify 'Main Page Title' is visible"]`. `action` field should be null.
- **If recovery seems impossible, too complex, or unsafe:** Set the `action` field to `"abort"` and provide a brief explanation in the `reasoning` field. `recovery_steps` should be null. Example: `{{"action": "abort", "reasoning": "Critical error page displayed, cannot identify recovery elements."}}`

**JSON Output Structure Examples:**
*Recovery Possible:*
```json
{{
  "recovery_steps": ["Click element 'Accept Cookies Button'", "Verify 'Main Page Title' is visible"],
  "action": null,
  "reasoning": null
}}
```
*Recovery Impossible:*
```json
{{
  "recovery_steps": null,
  "action": "abort",
  "reasoning": "The application crashed, unable to proceed."
}}
```
Respond ONLY with the JSON object matching the schema.
"""

        # --- Call LLM ---
        # --- Call LLM using generate_json, passing image_bytes ---
        response_obj = None
        error_msg = None
        try:
             # <<< START CHANGE >>>
             logger.debug("[LLM REPLAN] Sending prompt (and potentially image) to generate_json...")
             response_obj = self.gemini_client.generate_json(
                 ReplanSchema,
                 prompt,
                 image_bytes=screenshot_bytes # Pass image here
             )
             logger.debug(f"[LLM REPLAN] Raw response object type: {type(response_obj)}")
             # <<< END CHANGE >>>

        except Exception as e:
             logger.error(f"LLM call failed during re-planning: {e}", exc_info=True)
             print("Error: Could not communicate with LLM for re-planning.")
             error_msg = f"LLM communication error: {e}"

        # --- Parse Response ---
        recovery_steps = None
        abort_action = False
        abort_reasoning = "No reason provided."

        if isinstance(response_obj, ReplanSchema):
             logger.debug(f"[LLM REPLAN] Successfully parsed response: {response_obj}")
             if response_obj.recovery_steps and isinstance(response_obj.recovery_steps, list):
                  if all(isinstance(s, str) and s for s in response_obj.recovery_steps):
                       recovery_steps = response_obj.recovery_steps
                  else:
                       logger.warning(f"[LLM REPLAN] Parsed recovery_steps list contains invalid items: {response_obj.recovery_steps}")
                       error_msg = "LLM provided invalid recovery steps."
             elif response_obj.action == "abort":
                  abort_action = True
                  abort_reasoning = response_obj.reasoning or "No specific reason provided by AI."
                  logger.warning(f"AI recommended aborting recording. Reason: {abort_reasoning}")
             else:
                  logger.warning("[LLM REPLAN] LLM response did not contain valid recovery steps or an abort action.")
                  error_msg = "LLM response was valid JSON but lacked recovery_steps or abort action."

        elif isinstance(response_obj, str): # Handle error string from generate_json
             logger.error(f"[LLM REPLAN] Failed to generate/parse recovery JSON: {response_obj}")
             error_msg = f"LLM generation/parsing error: {response_obj}"
        elif response_obj is None and error_msg: # Handle communication error from above
             pass # error_msg already set
        else: # Handle unexpected return type
             logger.error(f"[LLM REPLAN] Unexpected response type from generate_json: {type(response_obj)}")
             error_msg = f"Unexpected response type: {type(response_obj)}"

        # --- Handle Outcome (Mode-dependent) ---
        if abort_action:
            print(f"\nAI Suggests Aborting: {abort_reasoning}")
            if self.automated_mode:
                print("[Auto Mode] Accepting AI abort suggestion.")
                abort_choice = 'a'
            else:
                abort_choice = input("AI suggests aborting. Abort (A) or Ignore and Skip Failed Step (S)? > ").strip().lower()

            if abort_choice == 'a':
                 self._user_abort_recording = True # Mark for abort
                 self.task_manager.update_subtask_status(current_planned_task['index'], "failed", error=f"Aborted based on AI re-planning suggestion: {abort_reasoning}", force_update=True)
                 return False # Abort
            else: # Skipped (Interactive only)
                 logger.info("User chose to ignore AI abort suggestion and skip the failed step.")
                 self.task_manager.update_subtask_status(current_planned_task['index'], "skipped", result="Skipped after AI suggested abort", force_update=True)
                 return False # Didn't insert steps

        elif recovery_steps:
            print("\nAI Suggested Recovery Steps:")
            for i, step in enumerate(recovery_steps): print(f"  {i+1}. {step}")

            if self.automated_mode:
                print("[Auto Mode] Accepting AI recovery steps.")
                confirm_recovery = 'y'
            else:
                confirm_recovery = input("Attempt these recovery steps? (Y/N/Abort) > ").strip().lower()

            if confirm_recovery == 'y':
                 logger.info(f"Attempting AI recovery steps: {recovery_steps}")
                 if self._insert_recovery_steps(current_planned_task['index'], recovery_steps):
                      self._consecutive_suggestion_failures = 0
                      return True # Indicate recovery steps were inserted
                 else: # Insertion failed (should be rare)
                      print("Error: Failed to insert recovery steps. Skipping original failed step.")
                      self.task_manager.update_subtask_status(current_planned_task['index'], "skipped", result="Skipped (failed to insert AI recovery steps)", force_update=True)
                      return False
            elif confirm_recovery == 'a': # Interactive only
                 self._user_abort_recording = True
                 return False # Abort
            else: # N or invalid (Interactive or failed auto-acceptance)
                 print("Skipping recovery attempt and the original failed step.")
                 logger.info("User declined/skipped AI recovery steps. Skipping original failed step.")
                 self.task_manager.update_subtask_status(current_planned_task['index'], "skipped", result="Skipped (User/Auto declined AI recovery)", force_update=True)
                 return False # Skipped

        else: # LLM failed to provide valid steps or abort
            print(f"\nAI failed to provide valid recovery steps or an abort action. Reason: {error_msg or 'Unknown LLM issue'}")
            if self.automated_mode:
                print("[Auto Mode] Skipping failed step due to LLM re-planning failure.")
                skip_choice = 's'
            else:
                skip_choice = input("Skip the current failed step (S) or Abort recording (A)? > ").strip().lower()

            if skip_choice == 'a': # Interactive only possibility
                 self._user_abort_recording = True
                 return False # Abort
            else: # Skip (default for auto mode, or user choice)
                 print("Skipping the original failed step.")
                 logger.warning(f"LLM failed re-planning ({error_msg}). Skipping original failed step.")
                 self.task_manager.update_subtask_status(current_planned_task['index'], "skipped", result=f"Skipped (AI re-planning failed: {error_msg})", force_update=True)
                 return False # Skipped


    def _insert_recovery_steps(self, index: int, recovery_steps: List[str]) -> bool:
        """Calls TaskManager to insert steps."""
        return self.task_manager.insert_subtasks(index, recovery_steps)

    def _determine_action_and_selector_for_recording(self,
                               current_task: Dict[str, Any],
                               current_url: str,
                               dom_context_str: str # Now contains indexed elements with PRE-GENERATED selectors
                               ) -> Optional[Dict[str, Any]]: # Keep return type as Dict for downstream compatibility
        """
        Uses LLM (generate_json) to propose the browser action (click, type) and identify the target *element index*
        based on the planned step description and the DOM context. The robust selector is retrieved
        from the DOM state afterwards. Returns a dictionary representation or None on error.
        """
        logger.info(f"Determining AI suggestion for planned step: '{current_task['description']}'")

        # --- Modified Prompt for Recorder using generate_json ---
        prompt = f"""
You are an AI assistant helping a user record a web test. Your goal is to interpret the user's planned step and identify the **single target interactive element** in the provided context that corresponds to it, then suggest the appropriate action.

**Feature Under Test:** {self.feature_description}
**Current Planned Step:** {current_task['description']}
**Current URL:** {current_url}
**Test Recording Progress:** Attempt {current_task['attempts']} of {self.task_manager.max_retries_per_subtask + 1} for this suggestion.

**Input Context (Visible Interactive Elements with Indices):**
This section shows visible interactive elements on the page, each marked with `[index]` and its pre-generated robust CSS selector.
```html
{dom_context_str}
```

**Your Task:**
Based ONLY on the "Current Planned Step" description and the "Input Context":
1.  Determine the appropriate **action** (`click`, `type`, `check`, `uncheck`, `action_not_applicable`, `suggestion_failed`).
2.  If action is `click` or `type` or `check` or `uncheck`:
    *   Identify the **single most likely interactive element `[index]`** from the context that matches the description. Set `parameters.index`.
3.  If action is `type`: Extract the **text** to be typed. Set `parameters.text`.
4.  Provide brief **reasoning** linking the step description to the chosen index/action.

**Output JSON Structure Examples:**

*Click Action:*
```json
{{
  "action": "click",
  "parameters": {{"index": 12}},
  "reasoning": "The step asks to click the 'Login' button, which corresponds to element [12]."
}}
```
*Type Action:*
```json
{{
  "action": "type",
  "parameters": {{"index": 5, "text": "user@example.com"}},
  "reasoning": "The step asks to type 'user@example.com' into the email field, which is element [5]."
}}
```
*Check Action:* 
```json
{{ 
    "action": "check", 
    "parameters": {{"index": 8}}, 
    "reasoning": "Step asks to check the 'Agree' checkbox [8]." 
}}
```
*Uncheck Action:* 
```json
{{
    "action": "uncheck", 
    "parameters": {{"index": 9}}, 
    "reasoning": "Step asks to uncheck 'Subscribe' [9]." 
}}
```
*Not Applicable (Navigation/Verification):*
```json
{{
  "action": "action_not_applicable",
  "parameters": {{}},
  "reasoning": "The step 'Navigate to ...' does not involve clicking or typing on an element from the context."
}}
```
*Suggestion Failed (Cannot identify element):*
```json
{{
  "action": "suggestion_failed",
  "parameters": {{}},
  "reasoning": "Could not find a unique element matching 'the second confirmation button'."
}}
```

**CRITICAL INSTRUCTIONS:**
-   Focus on the `[index]` for `click`/`type` actions.
-   Do NOT output selectors.
-   Use `action_not_applicable` for navigation, verification, scroll steps.
-   Be precise with extracted `text` for the `type` action.

Respond ONLY with the JSON object matching the schema.
"""
        # --- End Prompt ---

        # Add error context if retrying suggestion
        if current_task['status'] == 'in_progress' and current_task['attempts'] > 1 and current_task.get('error'):
            error_context = str(current_task['error'])[:300] + "..."
            prompt += f"\n**Previous Suggestion Attempt Error:**\nAttempt {current_task['attempts'] - 1} failed: {error_context}\nRe-evaluate the description and context carefully.\n"

        # Add history summary for general context
        prompt += f"\n**Recent History (Context):**\n{self._get_history_summary()}\n"

        logger.debug(f"[LLM RECORDER PROMPT] Sending prompt snippet for action/index suggestion:\n{prompt[:500]}...")

        response_obj = self.gemini_client.generate_json(RecorderSuggestionSchema, prompt)

        suggestion_dict = None # Initialize
        suggestion_failed = False
        failure_reason = "LLM suggestion generation failed."

        if isinstance(response_obj, RecorderSuggestionSchema):
            logger.debug(f"[LLM RECORDER RESPONSE] Parsed suggestion: {response_obj}")
            # Convert to dict for downstream use (or refactor downstream to use object)
            suggestion_dict = response_obj.model_dump(exclude_none=True)
            action = suggestion_dict.get("action")
            reasoning = suggestion_dict.get("reasoning", "No reasoning provided.")
            logger.info(f"[LLM Suggestion] Action: {action}, Params: {suggestion_dict.get('parameters')}, Reasoning: {reasoning[:150]}...")
            self._add_to_history("LLM Suggestion", suggestion_dict)

            # --- Basic Validation (Schema handles enum/types) ---
            required_index_actions = ["click", "type", "check", "uncheck"]
            if action in required_index_actions:
                target_index = suggestion_dict.get("parameters", {}).get("index")
                if target_index is None: # Index is required for these actions
                    logger.error(f"LLM suggested action '{action}' but missing required index.")
                    suggestion_failed = True
                    failure_reason = f"LLM suggestion '{action}' missing required parameter 'index'."
                elif action == "type" and suggestion_dict.get("parameters", {}).get("text") is None:
                    logger.error(f"LLM suggested action 'type' but missing required text.")
                    suggestion_failed = True
                    failure_reason = f"LLM suggestion 'type' missing required parameter 'text'."

            elif action == "suggestion_failed":
                  suggestion_failed = True
                  failure_reason = suggestion_dict.get("reasoning", "LLM indicated suggestion failed.")

            elif action == "action_not_applicable":
                  pass # This is a valid outcome, handled below

            else: # Should not happen if schema enum is enforced
                  logger.error(f"LLM returned unexpected action type: {action}")
                  suggestion_failed = True
                  failure_reason = f"LLM returned unknown action '{action}'."

        elif isinstance(response_obj, str): # Handle error string
             logger.error(f"[LLM Suggestion Failed] LLM returned an error string: {response_obj}")
             self._add_to_history("LLM Suggestion Failed", {"raw_error_response": response_obj})
             suggestion_failed = True
             failure_reason = f"LLM error: {response_obj}"
        else: # Handle unexpected type
             logger.error(f"[LLM Suggestion Failed] Unexpected response type from generate_json: {type(response_obj)}")
             self._add_to_history("LLM Suggestion Failed", {"response_type": str(type(response_obj))})
             suggestion_failed = True
             failure_reason = f"Unexpected response type: {type(response_obj)}"

        # --- Process Suggestion ---
        if suggestion_failed:
             # Return a standardized failure dictionary
             return {"action": "suggestion_failed", "parameters": {}, "reasoning": failure_reason}

        # Handle successful suggestions (click, type, not_applicable)
        if suggestion_dict["action"] in required_index_actions:
            target_index = suggestion_dict["parameters"]["index"] # We validated index exists above

            # --- Retrieve the node and pre-generated selector ---
            if self._latest_dom_state is None or not self._latest_dom_state.selector_map:
                 logger.error("DOM state or selector map is missing, cannot lookup suggested index.")
                 return {"action": "suggestion_failed", "parameters": {}, "reasoning": "Internal error: DOM state unavailable."}

            target_node = self._latest_dom_state.selector_map.get(target_index)
            if target_node is None:
                 available_indices = list(self._latest_dom_state.selector_map.keys())
                 logger.error(f"LLM suggested index [{target_index}], but it was not found in DOM context map. Available: {available_indices}")
                 return {"action": "suggestion_failed", "parameters": {}, "reasoning": f"Suggested element index [{target_index}] not found in current page context."}

            suggested_selector = target_node.css_selector
            if not suggested_selector:
                 # Try to generate it now if missing
                 suggested_selector = self.browser_controller.get_selector_for_node(target_node)
                 if suggested_selector:
                      target_node.css_selector = suggested_selector # Cache it
                 else:
                      logger.error(f"Could not generate selector for suggested index [{target_index}] (Node: {target_node.tag_name}).")
                      return {"action": "suggestion_failed", "parameters": {}, "reasoning": f"Failed to generate CSS selector for suggested index [{target_index}]."}

            logger.info(f"LLM suggested index [{target_index}], resolved to selector: '{suggested_selector}'")
            # Add resolved selector and node to the dictionary returned
            suggestion_dict["suggested_selector"] = suggested_selector
            suggestion_dict["target_node"] = target_node
            return suggestion_dict

        elif suggestion_dict["action"] == "action_not_applicable":
             # Pass this through directly
             return suggestion_dict

        else: # Should be unreachable given the checks above
             logger.error("Reached unexpected point in suggestion processing.")
             return {"action": "suggestion_failed", "parameters": {}, "reasoning": "Internal processing error after LLM response."}


    def _execute_action_for_recording(self, action: str, selector: Optional[str], parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes a specific browser action (navigate, click, type) during recording.
        This is called *after* user confirmation/override. It does not involve AI decision.
        """
        result = {"success": False, "message": f"Action '{action}' invalid.", "data": None}

        if not action:
            result["message"] = "No action specified for execution."
            logger.warning(f"[RECORDER_EXEC] {result['message']}")
            return result

        logger.info(f"[RECORDER_EXEC] Executing: {action} | Selector: {selector} | Params: {parameters}")
        self._add_to_history("Executing Recorder Action", {"action": action, "selector": selector, "parameters": parameters})

        try:
            if action == "navigate":
                url = parameters.get("url")
                if not url or not isinstance(url, str): raise ValueError("Missing or invalid 'url'.")
                self.browser_controller.goto(url)
                result["success"] = True
                result["message"] = f"Navigated to {url}."
                # Add implicit wait for load state after navigation
                self.recorded_steps.append({
                    "step_id": self._current_step_id, # Use internal counter
                    "action": "wait_for_load_state",
                    "description": "Wait for page navigation to complete",
                    "parameters": {"state": "domcontentloaded"}, # Reasonable default
                    "selector": None,
                    "wait_after_secs": 0
                })
                self._current_step_id += 1 # Increment after adding implicit step

            elif action == "click":
                if not selector: raise ValueError("Missing selector for click action.")
                self.browser_controller.click(selector)
                result["success"] = True
                result["message"] = f"Clicked element: {selector}."

            elif action == "type":
                text = parameters.get("text")
                if not selector: raise ValueError("Missing selector for type action.")
                if text is None: raise ValueError("Missing or invalid 'text'.") # Allow empty string? yes.
                self.browser_controller.type(selector, text)
                result["success"] = True
                result["message"] = f"Typed into element: {selector}."

            elif action == "scroll": # Basic scroll support if planned
                direction = parameters.get("direction")
                if direction not in ["up", "down"]: raise ValueError("Invalid scroll direction.")
                self.browser_controller.scroll(direction)
                result["success"] = True
                result["message"] = f"Scrolled {direction}."
            
            elif action == "check": 
                 if not selector: raise ValueError("Missing selector for check action.")
                 self.browser_controller.check(selector)
                 result["success"] = True
                 result["message"] = f"Checked element: {selector}."

            elif action == "uncheck": 
                 if not selector: raise ValueError("Missing selector for uncheck action.")
                 self.browser_controller.uncheck(selector)
                 result["success"] = True
                 result["message"] = f"Unchecked element: {selector}."
            
            else:
                result["message"] = f"Action '{action}' is not directly executable during recording via this method."
                logger.warning(f"[RECORDER_EXEC] {result['message']}")


        except (PlaywrightError, PlaywrightTimeoutError, ValueError) as e:
            error_msg = f"Execution during recording failed for action '{action}' on selector '{selector}': {type(e).__name__}: {e}"
            logger.error(f"[RECORDER_EXEC] {error_msg}", exc_info=False)
            result["message"] = error_msg
            result["success"] = False
             # Optionally save screenshot on execution failure *during recording*
            try:
                ts = time.strftime("%Y%m%d_%H%M%S")
                fname = f"output/recorder_exec_fail_{action}_{ts}.png"
                self.browser_controller.save_screenshot(fname)
                logger.info(f"Saved screenshot on recorder execution failure: {fname}")
            except: pass # Ignore screenshot errors here

        except Exception as e:
            error_msg = f"Unexpected Error during recorder execution action '{action}': {type(e).__name__}: {e}"
            logger.critical(f"[RECORDER_EXEC] {error_msg}", exc_info=True)
            result["message"] = error_msg
            result["success"] = False

        # Log Action Result
        log_level = logging.INFO if result["success"] else logging.WARNING
        logger.log(log_level, f"[RECORDER_EXEC_RESULT] Action '{action}' | Success: {result['success']} | Message: {result['message']}")
        self._add_to_history("Recorder Action Result", {"success": result["success"], "message": result["message"]})

        return result

    # --- New Recorder Core Logic ---

    def _handle_interactive_step_recording(self, planned_step: Dict[str, Any], suggestion: Dict[str, Any]) -> bool:
        """
        Handles the user interaction loop for a suggested 'click' or 'type' action.
        Returns True if the step was successfully recorded (or skipped), False if aborted.
        """
        action = suggestion["action"]
        suggested_selector = suggestion["suggested_selector"]
        target_node = suggestion["target_node"] # DOMElementNode
        parameters = suggestion["parameters"] # Contains index and potentially text
        reasoning = suggestion.get("reasoning", "N/A")
        planned_desc = planned_step["description"]

        final_selector = None
        performed_action = False
        # should_retry_suggestion = False # Flag to indicate if execution failure should lead to retry    

        # --- Automated Mode ---
        if self.automated_mode:
            logger.info(f"[Auto Mode] Handling AI suggestion: Action='{action}', Target='{target_node.tag_name}' (Reason: {reasoning})")
            logger.info(f"[Auto Mode] Suggested Selector: {suggested_selector}")

            # Directly accept AI suggestion
            final_selector = suggested_selector
            logger.info(f"[Auto Mode] Automatically accepting AI suggestion.")

            # Execute action on AI's suggested selector
            exec_result = self._execute_action_for_recording(action, final_selector, parameters)
            performed_action = exec_result["success"]

            if performed_action:
                 # Record the successful step automatically
                 record = {
                    "step_id": self._current_step_id, "action": action, "description": planned_desc,
                    "parameters": {}, "selector": final_selector, "wait_after_secs": DEFAULT_WAIT_AFTER_ACTION
                 }
                 if action == "type":
                     # Include text, but no parameterization prompt
                     record["parameters"]["text"] = parameters.get("text", "")
                 self.recorded_steps.append(record)
                 self._current_step_id += 1
                 logger.info(f"Step {record['step_id']} recorded (AI Suggestion - Automated): {action} on {final_selector}")
                 self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "done", result=f"Recorded AI suggestion (automated) as step {record['step_id']}")
                 return True # Success

            else: # AI suggestion execution failed
                 logger.error(f"[Auto Mode] Execution FAILED using AI suggested selector: {exec_result['message']}")
                 # Mark as failed for potential re-planning or retry in the main loop
                 self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "failed", error=f"Automated execution failed: {exec_result['message']}")
                 # Do not abort automatically, let the main loop handle failure/retry logic
                 return True # Indicate handled (failure noted), loop continues
         # --- Interactive Mode ---
        else:
            
        
            print("\n" + "="*60)
            print(f"Planned Step: {planned_desc}")
            print(f"AI Suggestion: Action='{action}', Target='{target_node.tag_name}' (Reason: {reasoning})")
            print(f"Suggested Selector: {suggested_selector}")
            print("="*60)

            # Highlight suggested element
            self.browser_controller.clear_highlights()
            self.browser_controller.highlight_element(suggested_selector, target_node.highlight_index, color="#FFA500", text="AI Suggestion") # Orange for suggestion

            # Setup listener *before* prompt
            listener_setup = self.browser_controller.setup_click_listener()
            if not listener_setup:
                logger.error("Failed to set up click listener, cannot proceed with override.")
                # Optionally fallback to just accepting AI suggestion? Or abort? Let's abort for safety.
                return False # Indicate failure/abort


            override_selector = None

            # Use a separate thread for input to allow click detection simultaneously
            # Note: This basic input approach might block other activities if not handled carefully.
            # Consider more robust async/event-driven approaches for complex GUIs.
            try:
                logger.debug("Calling wait_for_user_click_or_timeout (wait_for_function version)...")
                # Call the updated wait function - it now returns the selector string or None
                override_selector = self.browser_controller.wait_for_user_click_or_timeout(INTERACTIVE_TIMEOUT_SECS)
                logger.debug(f"Returned from wait_for_user_click_or_timeout. override_selector = {override_selector}")

                # --- Handle Override First ---
                if override_selector is not None: # Check if a selector string was returned
                    print(f"\n[Recorder] User override detected! Using selector: {override_selector}")
                    final_selector = override_selector
                    performed_action = False

                    # Execute the original *intended* action on the *overridden* selector
                    print(f"Executing original action '{action}' on overridden selector...")
                    exec_result = self._execute_action_for_recording(action, final_selector, parameters)
                    performed_action = exec_result["success"]

                    if performed_action:
                        # --- Record the successful override step ---
                        record = {
                            "step_id": self._current_step_id,
                            "action": action,
                            "description": planned_desc,
                            "parameters": {},
                            "selector": final_selector,
                            "wait_after_secs": DEFAULT_WAIT_AFTER_ACTION
                        }
                        if action == "type":
                            record["parameters"]["text"] = parameters.get("text", "")
                            param_name_input = input(f"  Parameterize value '{parameters.get('text')}'? Enter name or leave blank: ").strip()
                            if param_name_input: record["parameters"]["parameter_name"] = param_name_input
                        self.recorded_steps.append(record)
                        self._current_step_id += 1
                        logger.info(f"Step {record['step_id']} recorded (User Override): {action} on {final_selector}")
                        self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "done", result=f"Recorded override as step {record['step_id']}")
                        return True # <<<--- RETURN HERE: Override successful bypass console prompt

                    else: # Override execution failed
                        print(f"WARNING: Execution failed using override selector: {exec_result['message']}")
                        retry_choice = input("Override execution failed. Skip (S) or Abort (A)? > ").strip().lower()
                        if retry_choice == 'a':
                            self._user_abort_recording = True
                            return False # Abort
                        else:
                            print("Skipping step after failed override execution.")
                            self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "skipped", result="Skipped after failed override execution")
                            return True # Skip, RETURN HERE Bypass console prompt
                else:
                    logger.debug("No click override registered (wait_for_function timed out), prompting user via console.")
                    prompt = (
                        f"AI suggests '{action}' on the highlighted element with selector `{suggested_selector}`.\n"
                        f"  Accept suggestion? (Press Enter or Y)\n"
                        f"  Skip this step? (S)\n"
                        f"  Abort recording? (A)\n"
                        f"Your choice? > "
                    )
                    user_choice = input(prompt).strip().lower()

                    # --- Process Console User Choice ---
                    if user_choice == '' or user_choice == 'y':
                        print("Accepting AI suggestion.")
                        final_selector = suggested_selector
                        performed_action = False # Initialize

                        # Execute action on AI's suggested selector
                        exec_result = self._execute_action_for_recording(action, final_selector, parameters)
                        performed_action = exec_result["success"]

                        if performed_action:
                            # --- Record the successful AI suggestion step ---
                            record = {
                                "step_id": self._current_step_id,
                                "action": action,
                                "description": planned_desc, # Use original description
                                "parameters": {},
                                "selector": final_selector,
                                "wait_after_secs": DEFAULT_WAIT_AFTER_ACTION
                            }
                            if action == "type":
                                record["parameters"]["text"] = parameters.get("text", "")
                                # Ask for parameterization
                                param_name_input = input(f"  Parameterize value '{parameters.get('text')}'? Enter name or leave blank: ").strip()
                                if param_name_input:
                                    record["parameters"]["parameter_name"] = param_name_input

                            self.recorded_steps.append(record)
                            self._current_step_id += 1
                            logger.info(f"Step {record['step_id']} recorded (AI Suggestion): {action} on {final_selector}")
                            self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "done", result=f"Recorded AI suggestion as step {record['step_id']}")
                            return True # Success

                        else: # AI suggestion execution failed
                            print(f"WARNING: Execution failed using AI suggested selector: {exec_result['message']}")
                            retry_choice = input("Execution failed. Retry suggestion (R), Skip (S) or Abort (A)? > ").strip().lower()
                            if retry_choice == 'a':
                                self._user_abort_recording = True
                                return False # Abort
                            elif retry_choice == 'r':
                                print("Marking step for retry...")
                                current_task_index = self.task_manager.current_subtask_index
                                self.task_manager.update_subtask_status(current_task_index, "failed", error="User requested retry after execution failure.")
                                return True # Signal to loop to retry planning/suggestion
                            else:
                                print("Skipping step after failed execution.")
                                self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "skipped", result="Skipped after failed AI suggestion execution")
                                return True # Skip

                    elif user_choice == 's':
                        print("Skipping planned step.")
                        self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "skipped", result="User skipped")
                        return True # Skip

                    elif user_choice == 'a':
                        print("Aborting recording process.")
                        self._user_abort_recording = True
                        return False # Abort

                    else:
                        print("Invalid choice. Skipping step.")
                        self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "skipped", result="Invalid user choice")
                        return True # Skip

            except Exception as e:
                logger.error(f"Error during user interaction: {e}", exc_info=True)
                user_choice = 'a' # Abort on error

            # --- Process User Choice ---
            # Now use override_selector which was set if final_clicked_selector was found
            if user_choice == 'override' or override_selector:
                print(f"Using override selector: {override_selector}")
                final_selector = override_selector
                # Execute the original *intended* action on the *overridden* selector
                print(f"Executing original action '{action}' on overridden selector...")
                exec_result = self._execute_action_for_recording(action, final_selector, parameters)
                performed_action = exec_result["success"]
                if not performed_action:
                    print(f"WARNING: Execution failed using override selector: {exec_result['message']}")
                    retry_choice = input("Execution failed. Skip (S) or Abort (A)? > ").strip().lower()
                    if retry_choice == 'a':
                        self._user_abort_recording = True
                        return False # Abort
                    else:
                        print("Skipping step after failed override execution.")
                        self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "skipped", result="Skipped after failed override execution")
                        return True # Skip


            elif user_choice == '' or user_choice == 'y':
                print("Accepting AI suggestion.")
                final_selector = suggested_selector
                # Execute action on AI's suggested selector
                exec_result = self._execute_action_for_recording(action, final_selector, parameters)
                performed_action = exec_result["success"]
                if not performed_action:
                    print(f"WARNING: Execution failed using AI suggested selector: {exec_result['message']}")
                    retry_choice = input("Execution failed. Retry suggestion (R), Skip (S) or Abort (A)? > ").strip().lower()
                    if retry_choice == 'a':
                        self._user_abort_recording = True
                        return False # Abort
                    elif retry_choice == 'r':
                        print("Marking step for retry...")
                        current_task_index = self.task_manager.current_subtask_index
                        self.task_manager.update_subtask_status(current_task_index, "failed", error="User requested retry after execution failure.")
                        # Important: Ensure no step is recorded when retrying
                        return True # Signal to loop to retry planning/suggestion
                    else:
                        print("Skipping step after failed execution.")
                        self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "skipped", result="Skipped after failed AI suggestion execution")
                        return True # Skip
                else:
                    self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "done", result="Executed AI suggestion")

            elif user_choice == 's':
                print("Skipping planned step.")
                # Mark task manager step as skipped? Or just don't record. Let's not record.
                self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "skipped", result="User skipped")
                performed_action = False # No action performed or recorded
                return True # Continue to next planned step

            elif user_choice == 'a':
                print("Aborting recording process.")
                self._user_abort_recording = True
                return False # Signal abort to main loop

            else:
                print("Invalid choice. Skipping step.")
                self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "skipped", result="Invalid user choice")
                performed_action = False
                return True # Continue to next planned step

            # --- Record Step if Action Performed ---
            if performed_action and final_selector:
                record = {
                    "step_id": self._current_step_id,
                    "action": action,
                    "description": planned_desc, # Use original planned description
                    "parameters": {}, # Initialize empty params
                    "selector": final_selector,
                    "wait_after_secs": DEFAULT_WAIT_AFTER_ACTION # Add small default wait
                }
                if action == "type":
                    record["parameters"]["text"] = parameters.get("text", "") # Get text from original params
                    # Add parameterization hook (simple example)
                    param_name_input = input(f"  Parameterize value '{parameters.get('text')}'? Enter name (e.g., username) or leave blank: ").strip()
                    if param_name_input:
                        record["parameters"]["parameter_name"] = param_name_input

                self.recorded_steps.append(record)
                self._current_step_id += 1
                logger.info(f"Step {record['step_id']} recorded: {action} on {final_selector}")
                return True # Success
            elif final_selector: # Action wasn't performed (e.g., failed override) but we decided not to abort/skip
                # This case should be handled by the return False or True for skip/abort above
                logger.warning("Reached unexpected state where action wasn't performed but step wasn't skipped/aborted.")
                return True # Treat as skipped
            else: # No selector finalized (e.g., abort/skip)
                return True # Already handled by returns above


    def _handle_assertion_recording(self, planned_step: Dict[str, Any]) -> bool:
        """
        Handles prompting the user for assertion details based on a 'Verify...' planned step.
        Returns True if recorded/skipped, False if aborted.
        """
        if self.automated_mode:
            logger.error("[Auto Mode] Reached manual assertion handler. This indicates verification fallback failed or wasn't triggered. Skipping step.")
            self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "skipped", result="Skipped (Manual assertion handler reached in auto mode)")
            return True # Skip

        planned_desc = planned_step["description"]
        print("\n" + "="*60)
        print(f"Planned Step: {planned_desc}")
        print("This is a verification step. Let's define the assertion.")
        print("="*60)

        # 1. Identify Target Element (Use LLM again, simplified prompt)
        #    We need a selector for the element to assert against.
        current_url = self.browser_controller.get_current_url()
        dom_context_str = "Error getting DOM"
        if self._latest_dom_state:
            dom_context_str = self._latest_dom_state.element_tree.generate_llm_context_string(context_purpose='verification')

        prompt = f"""
        Given the verification step: "{planned_desc}"
        And the current visible interactive elements context (with indices):
        ```html
        {dom_context_str}
        ```
        Identify the element index `[index]` most relevant to this verification task.
        Respond ONLY with a JSON object matching the schema:
        {{
            "index": INDEX_NUMBER_OR_NULL,
            "reasoning": "OPTIONAL_REASONING_IF_NULL"
        }}

        Example Output (Found): {{"index": 5}}
        Example Output (Not Found): {{"index": null, "reasoning": "Cannot determine a single target element for 'verify presence of error'."}}
        """
        logger.debug(f"[LLM ASSERT PROMPT] Sending prompt for assertion target index:\n{prompt[:500]}...")

        response_obj = self.gemini_client.generate_json(AssertionTargetIndexSchema, prompt)

        target_index = None
        llm_reasoning = "LLM did not provide a target index or reasoning." # Default

        if isinstance(response_obj, AssertionTargetIndexSchema):
             logger.debug(f"[LLM ASSERT RESPONSE] Parsed index response: {response_obj}")
             target_index = response_obj.index # Will be None if null in JSON
             if target_index is None and response_obj.reasoning:
                  llm_reasoning = response_obj.reasoning
             elif target_index is None:
                   llm_reasoning = "LLM did not identify a target element (index is null)."

        elif isinstance(response_obj, str): # Handle error string
             logger.error(f"[LLM ASSERT RESPONSE] Failed to get target index JSON: {response_obj}")
             llm_reasoning = f"LLM error getting target index: {response_obj}"
        else: # Handle unexpected type
             logger.error(f"[LLM ASSERT RESPONSE] Unexpected response type for target index: {type(response_obj)}")
             llm_reasoning = f"Unexpected LLM response type: {type(response_obj)}"

        target_node = None
        target_selector = None

        if target_index is not None: # Check explicitly for non-None integer
            if self._latest_dom_state and self._latest_dom_state.selector_map:
                 target_node = self._latest_dom_state.selector_map.get(target_index)
                 if target_node and target_node.css_selector:
                      target_selector = target_node.css_selector
                      print(f"AI suggests asserting on element [Index: {target_index}]: <{target_node.tag_name}> with selector: `{target_selector}`")
                      self.browser_controller.clear_highlights()
                      self.browser_controller.highlight_element(target_selector, target_index, color="#0000FF", text="Assert Target?") # Blue for assert
                 else:
                      print(f"AI suggested index [{target_index}], but element or selector not found in current context.")
                      target_index = None # Reset index as it's unusable
            else:
                 print(f"AI suggested index [{target_index}], but DOM context is unavailable.")
                 target_index = None # Reset index
        # else: target_index is None, use llm_reasoning below

        if target_index is None: # If index is still None after checks or initially null
            print(f"AI could not confidently identify a target element for '{planned_desc}'. Reason: {llm_reasoning}")


        # --- User confirms/overrides target selector ---
        override_prompt = "Enter a different selector if needed, or press Enter to use suggested/skip, or A to abort: > "
        user_input_selector = input(override_prompt).strip()

        if user_input_selector.lower() == 'a':
             self._user_abort_recording = True
             return False
        elif user_input_selector == '' and target_selector:
             final_selector = target_selector
             print(f"Using suggested selector: {final_selector}")
        elif user_input_selector != '':
             final_selector = user_input_selector
             print(f"Using user-provided selector: {final_selector}")
             # Optionally try to highlight the user's selector
             try:
                  self.browser_controller.clear_highlights()
                  self.browser_controller.highlight_element(final_selector, 0, color="#00FF00", text="User Target") # Green for user choice
             except Exception as e:
                  print(f"Warning: Could not highlight user selector '{final_selector}': {e}")
        else: # User hit Enter without suggestion or providing one
            print("No target selector specified. Skipping assertion.")
            self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "skipped", result="User skipped assertion target")
            return True # Skip


        # --- User Selects Assertion Type ---
        print("\nChoose Assertion Type:")
        print("  [1] Text Contains")
        print("  [2] Text Equals")
        print("  [3] Is Visible")
        print("  [4] Is Hidden (Not Visible)")
        print("  [5] Attribute Equals")
        print("  [6] Element Count Equals")
        print("  [7] Is Checked (Checkbox/Radio)")
        print("  [8] Is Not Checked (Checkbox/Radio)")
        print("  [S] Skip Assertion")
        print("  [A] Abort Recording")
        assert_choice = input("Enter choice: > ").strip().lower()

        assertion_action = None
        assertion_params = {}

        if assert_choice == '1':
            assertion_action = "assert_text_contains"
            expected_text = input("Enter expected text to contain: ").strip()
            assertion_params["expected_text"] = expected_text
        elif assert_choice == '2':
            assertion_action = "assert_text_equals"
            expected_text = input("Enter exact expected text: ").strip()
            assertion_params["expected_text"] = expected_text
        elif assert_choice == '3':
            assertion_action = "assert_visible"
        elif assert_choice == '4':
            assertion_action = "assert_hidden"
        elif assert_choice == '5':
            assertion_action = "assert_attribute_equals"
            attr_name = input("Enter attribute name (e.g., 'class', 'disabled'): ").strip()
            expected_value = input(f"Enter expected value for attribute '{attr_name}': ").strip()
            assertion_params["attribute_name"] = attr_name
            assertion_params["expected_value"] = expected_value
        elif assert_choice == '6':
             assertion_action = "assert_element_count"
             expected_count = input("Enter expected element count: ").strip()
             try:
                 assertion_params["expected_count"] = int(expected_count)
             except ValueError:
                 print("Invalid count. Skipping assertion.")
                 self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "skipped", result="Invalid assertion count")
                 return True # Skip
        elif assert_choice == '7':
            assertion_action = "assert_checked"
            # No parameters needed
        elif assert_choice == '8':
            assertion_action = "assert_not_checked"
            # No parameters needed
        elif assert_choice == 's':
            print("Skipping assertion.")
            self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "skipped", result="User skipped assertion")
            return True # Skip
        elif assert_choice == 'a':
            print("Aborting recording.")
            self._user_abort_recording = True
            return False # Abort
        else:
            print("Invalid assertion choice. Skipping assertion.")
            self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "skipped", result="Invalid assertion choice")
            return True # Skip

        # --- Record the Assertion Step ---
        if assertion_action and final_selector:
            record = {
                "step_id": self._current_step_id,
                "action": assertion_action,
                "description": planned_desc, # Use original planned description
                "parameters": assertion_params,
                "selector": final_selector,
                "wait_after_secs": 0 # Assertions usually don't need waits after
            }
            self.recorded_steps.append(record)
            self._current_step_id += 1
            logger.info(f"Step {record['step_id']} recorded: {assertion_action} on {final_selector}")
             # Mark original planned step as done in task manager
            self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "done", result=f"Recorded as assertion step {record['step_id']}")
            return True
        else:
            # Should have been handled by skip/abort logic above
            logger.warning("Reached end of assertion handling without recording, skipping.")
            self.task_manager.update_subtask_status(self.task_manager.current_subtask_index, "skipped", result="Assertion not fully defined")
            return True # Skip


    def record(self, feature_description: str) -> Dict[str, Any]:
        """
        Runs the interactive test recording process with LLM verification and dynamic re-planning.
        """
        if not self.is_recorder_mode:
             logger.error("Cannot run record() method when not in recorder mode.")
             return {"success": False, "message": "Agent not initialized in recorder mode."}

        automation_status = "Automated" if self.automated_mode else "Interactive"
        logger.info(f"--- Starting Test Recording ({automation_status}) --- Feature: {feature_description}")
        if not self.automated_mode:
            print(f"\n--- Starting Recording for Feature ({automation_status}) ---\n{feature_description}\n" + "-"*35)
        start_time = time.time()
        # Initialize recording status
        recording_status = {
            "success": False,
            "feature": feature_description,
            "message": "Recording initiated.",
            "output_file": None,
            "steps_recorded": 0,
            "duration_seconds": 0.0,
        }
        # Reset state for a new recording session
        self.history = []
        self.recorded_steps = []
        self._current_step_id = 1
        self.output_file_path = None
        self._latest_dom_state = None
        self._user_abort_recording = False
        self._consecutive_suggestion_failures = 0
        self._last_failed_step_index = -1

        try:
            logger.debug("[RECORDER] Starting browser controller...")
            self.browser_controller.start()
            self.browser_controller.clear_console_messages()

            self.task_manager.set_main_task(feature_description)
            logger.debug("[RECORDER] Planning initial steps...")
            self._plan_subtasks(feature_description) # Generates the list of planned steps

            if not self.task_manager.subtasks:
                 recording_status["message"] = "❌ Recording Planning Failed: No steps generated."
                 raise ValueError(recording_status["message"]) # Use ValueError for planning failure

            logger.info(f"Beginning interactive recording for {len(self.task_manager.subtasks)} initial planned steps...")
            iteration_count = 0 # General loop counter for safety
            MAX_RECORDING_ITERATIONS = self.max_iterations * 2 # Allow more iterations for potential recovery steps

            while iteration_count < MAX_RECORDING_ITERATIONS:
                iteration_count += 1
                planned_steps_count = len(self.task_manager.subtasks) # Get current count
                current_planned_task = self.task_manager.get_next_subtask()

                if self._user_abort_recording: # Abort check
                    recording_status["message"] = f"Recording aborted by {'user' if not self.automated_mode else 'automation logic'}."
                    logger.warning(recording_status["message"])
                    break

                if not current_planned_task:
                    # Check if finished normally or failed planning/retries
                    if self.task_manager.is_complete():
                         # Check if ANY task failed permanently
                         perm_failed_tasks = [t for t in self.task_manager.subtasks if t['status'] == 'failed' and t['attempts'] > self.task_manager.max_retries_per_subtask]
                         if perm_failed_tasks:
                              first_failed_idx = self.task_manager.subtasks.index(perm_failed_tasks[0])
                              failed_task = perm_failed_tasks[0]
                              recording_status["message"] = f"Recording process completed with failures. First failed step #{first_failed_idx+1}: {failed_task['description']} (Error: {failed_task['error']})"
                              recording_status["success"] = False # Mark as failed overall
                              logger.error(recording_status["message"])
                         elif all(t['status'] in ['done', 'skipped'] for t in self.task_manager.subtasks):
                              logger.info("All planned steps processed or skipped successfully.")
                              recording_status["message"] = "Recording process completed."
                              recording_status["success"] = True # Mark as success ONLY if no permanent failures
                         else:
                              # Should not happen if is_complete is true and perm_failed is empty
                              recording_status["message"] = "Recording finished, but final state inconsistent."
                              recording_status["success"] = False
                              logger.warning(recording_status["message"])

                    else:
                         recording_status["message"] = "Recording loop ended unexpectedly (no actionable tasks found)."
                         recording_status["success"] = False
                         logger.error(recording_status["message"])
                    break # Exit loop

                # Add index to the task dictionary for easier reference
                current_task_index = self.task_manager.current_subtask_index
                current_planned_task['index'] = current_task_index

                logger.info(f"\n===== Processing Planned Step {current_task_index + 1}/{planned_steps_count} (Attempt {current_planned_task['attempts']}) =====")
                if not self.automated_mode: print(f"\nProcessing Step {current_task_index + 1}: {current_planned_task['description']}")

                # --- Reset Consecutive Failure Counter if step index changes ---
                if self._last_failed_step_index != current_task_index:
                    self._consecutive_suggestion_failures = 0
                self._last_failed_step_index = current_task_index # Update last processed index

                # --- State Gathering ---
                logger.info("Gathering browser state and structured DOM...")
                current_url = "Error: Could not get URL"
                dom_context_str = "Error: Could not process DOM"
                screenshot_bytes = None # Initialize screenshot bytes
                self._latest_dom_state = None
                self.browser_controller.clear_highlights() # Clear previous highlights

                try:
                    current_url = self.browser_controller.get_current_url()
                    # Always try to get DOM state
                    self._latest_dom_state = self.browser_controller.get_structured_dom(highlight_all_clickable_elements=False)
                    if self._latest_dom_state and self._latest_dom_state.element_tree:
                        dom_context_str = self._latest_dom_state.element_tree.generate_llm_context_string(context_purpose='verification')
                    else:
                        dom_context_str = "Error processing DOM structure."
                        logger.error("[RECORDER] Failed to get valid DOM state.")

                    # Get screenshot, especially useful for verification/re-planning
                    screenshot_bytes = self.browser_controller.take_screenshot()

                except Exception as e:
                    logger.error(f"Failed to gather browser state/DOM/Screenshot: {e}", exc_info=True)
                    dom_context_str = f"Error gathering state: {e}"
                    # Allow proceeding, LLM might handle navigation or re-planning might trigger

                # --- Handle Step Type ---
                planned_step_desc_lower = current_planned_task['description'].lower()
                step_handled_internally = False # Flag to indicate if step logic was fully handled here

                # --- 1. Verification Step ---
                if planned_step_desc_lower.startswith(("verify", "check", "assert")):
                    logger.info("Handling verification step using LLM...")
                    verification_result = self._get_llm_verification(
                        verification_description=current_planned_task['description'],
                        current_url=current_url,
                        dom_context_str=dom_context_str,
                        screenshot_bytes=screenshot_bytes
                    )
                    if verification_result:
                        # Handle user confirmation & recording based on LLM result
                        if not self._handle_llm_verification(current_planned_task, verification_result):
                             self._user_abort_recording = True # Abort only possible in interactive
                    else:
                        # LLM verification failed, fallback to manual
                        if self.automated_mode:
                            logger.error("[Auto Mode] LLM verification call failed. Skipping step.")
                            self.task_manager.update_subtask_status(current_task_index, "skipped", result="Skipped (LLM verification failed)")
                        else:
                            print("AI verification failed. Falling back to manual assertion definition.")
                            if not self._handle_assertion_recording(current_planned_task): # Manual handler
                                self._user_abort_recording = True
                    step_handled_internally = True # Verification is fully handled here or in called methods

                # --- 2. Navigation Step ---
                elif planned_step_desc_lower.startswith("navigate to"):
                    try:
                        parts = re.split("navigate to", current_planned_task['description'], maxsplit=1, flags=re.IGNORECASE)
                        if len(parts) > 1 and parts[1].strip():
                            url = parts[1].strip()
                            print(f"Action: Navigate to {url}")
                            exec_result = self._execute_action_for_recording("navigate", None, {"url": url})
                            if exec_result["success"]:
                                # Record navigation step + implicit wait
                                nav_step_id = self._current_step_id
                                self.recorded_steps.append({
                                    "step_id": nav_step_id, "action": "navigate", "description": current_planned_task['description'],
                                    "parameters": {"url": url}, "selector": None, "wait_after_secs": 0 # Wait handled by wait_for_load_state
                                })
                                self._current_step_id += 1
                                self.recorded_steps.append({ # Add implicit wait
                                    "step_id": self._current_step_id, "action": "wait_for_load_state", "description": "Wait for page navigation",
                                    "parameters": {"state": "domcontentloaded"}, "selector": None, "wait_after_secs": DEFAULT_WAIT_AFTER_ACTION
                                })
                                self._current_step_id += 1
                                logger.info(f"Steps {nav_step_id}, {self._current_step_id-1} recorded: navigate and wait")
                                self.task_manager.update_subtask_status(current_task_index, "done", result="Recorded navigation")
                                self._consecutive_suggestion_failures = 0 # Reset failure counter on success
                            else:
                                # NAVIGATION FAILED - Potential trigger for re-planning
                                logger.error(f"Navigation failed: {exec_result['message']}")
                                reason = f"Navigation to '{url}' failed: {exec_result['message']}"
                                # Try re-planning instead of immediate skip/abort
                                if self._trigger_re_planning(current_planned_task, reason):
                                     logger.info("Re-planning successful, continuing with recovery steps.")
                                     # Recovery steps inserted, loop will pick them up
                                else:
                                      # Re-planning failed or user aborted/skipped recovery
                                      if not self._user_abort_recording: # Check if abort wasn't the reason
                                           logger.warning("Re-planning failed or declined after navigation failure. Skipping original step.")
                                           # Status already updated by _trigger_re_planning if skipped/aborted
                        else:
                             raise ValueError("Could not parse URL after 'navigate to'.")
                    except Exception as nav_e:
                         logger.error(f"Error processing navigation step '{current_planned_task['description']}': {nav_e}")
                         reason = f"Error processing navigation step: {nav_e}"
                         if self._trigger_re_planning(current_planned_task, reason):
                              logger.info("Re-planning successful after navigation processing error.")
                         else:
                              if not self._user_abort_recording:
                                   logger.warning("Re-planning failed/declined. Marking original navigation step failed.")
                                   self.task_manager.update_subtask_status(current_task_index, "failed", error=reason) # Mark as failed if no recovery

                    step_handled_internally = True # Navigation handled

                # --- 3. Scroll Step ---
                elif planned_step_desc_lower.startswith("scroll"):
                    try:
                        direction = "down" if "down" in planned_step_desc_lower else "up" if "up" in planned_step_desc_lower else None
                        if direction:
                            print(f"Action: Scroll {direction}")
                            exec_result = self._execute_action_for_recording("scroll", None, {"direction": direction})
                            if exec_result["success"]:
                                self.recorded_steps.append({
                                     "step_id": self._current_step_id, "action": "scroll", "description": current_planned_task['description'],
                                     "parameters": {"direction": direction}, "selector": None, "wait_after_secs": 0.2
                                })
                                self._current_step_id += 1
                                logger.info(f"Step {self._current_step_id-1} recorded: scroll {direction}")
                                self.task_manager.update_subtask_status(current_task_index, "done", result="Recorded scroll")
                                self._consecutive_suggestion_failures = 0 # Reset failure counter
                            else:
                                 print(f"Optional scroll failed: {exec_result['message']}. Skipping recording.")
                                 self.task_manager.update_subtask_status(current_task_index, "skipped", result="Optional scroll failed")
                        else:
                             print(f"Could not determine scroll direction from: {current_planned_task['description']}. Skipping.")
                             self.task_manager.update_subtask_status(current_task_index, "skipped", result="Unknown scroll direction")
                    except Exception as scroll_e:
                         logger.error(f"Error handling scroll step: {scroll_e}")
                         self.task_manager.update_subtask_status(current_task_index, "failed", error=f"Scroll step failed: {scroll_e}") # Mark failed
                    step_handled_internally = True # Scroll handled

                # --- 4. Default: Assume Interactive Click/Type ---
                if not step_handled_internally:
                    # --- AI Suggestion ---
                    ai_suggestion = self._determine_action_and_selector_for_recording(
                        current_planned_task, current_url, dom_context_str
                    )

                    # --- Handle Suggestion Result ---
                    if not ai_suggestion or ai_suggestion.get("action") == "suggestion_failed":
                        reason = ai_suggestion.get("reasoning", "LLM failed to provide valid suggestion.") if ai_suggestion else "LLM suggestion generation failed."
                        logger.error(f"AI suggestion failed for step {current_task_index + 1}: {reason}")
                        self._consecutive_suggestion_failures += 1
                        # Check if we should try re-planning due to repeated failures
                        if self._consecutive_suggestion_failures > self.task_manager.max_retries_per_subtask:
                            logger.warning(f"Maximum suggestion retries exceeded for step {current_task_index + 1}. Triggering re-planning.")
                            replan_reason = f"AI failed to suggest an action/selector repeatedly for step: '{current_planned_task['description']}'. Last reason: {reason}"
                            if self._trigger_re_planning(current_planned_task, replan_reason):
                                logger.info("Re-planning successful after suggestion failures.")
                                # Loop continues with recovery steps
                            else:
                                # Re-planning failed or user aborted/skipped
                                if not self._user_abort_recording:
                                    logger.error("Re-planning failed/declined. Marking original step as failed permanently.")
                                    self.task_manager.update_subtask_status(current_task_index, "failed", error=f"Failed permanently after repeated suggestion errors and failed re-planning attempt. Last reason: {reason}", force_update=True)
                        else:
                            # Mark as failed for normal retry by TaskManager
                            self.task_manager.update_subtask_status(current_task_index, "failed", error=reason)
                            # Continue loop, TaskManager will offer retry if possible

                    elif ai_suggestion.get("action") == "action_not_applicable":
                        reason = ai_suggestion.get("reasoning", "Step not a click/type.")
                        logger.info(f"Planned step '{current_planned_task['description']}' determined not applicable by AI. Skipping. Reason: {reason}")
                        # Could this trigger re-planning? Maybe if it happens unexpectedly. For now, treat as skip.
                        self.task_manager.update_subtask_status(current_task_index, "skipped", result=f"Skipped non-interactive step ({reason})")
                        self._consecutive_suggestion_failures = 0 # Reset counter on skip

                    elif ai_suggestion.get("action") in ["click", "type", "check", "uncheck"]:
                        # --- Handle Interactive Step (Confirmation/Override/Execution) ---
                        # This method now returns True if handled (recorded, skipped, retry requested), False if aborted
                        # It also internally updates task status based on outcome.
                        handled_ok = self._handle_interactive_step_recording(current_planned_task, ai_suggestion)

                        if not handled_ok and self._user_abort_recording:
                             logger.warning("User aborted during interactive step handling.")
                             break # Exit main loop immediately on abort

                        # Check if the step failed execution and might need re-planning
                        current_task_status = self.task_manager.subtasks[current_task_index]['status']
                        if current_task_status == 'failed':
                            # _handle_interactive_step_recording marks failed if execution fails and user doesn't skip/abort
                            # Check if it was an execution failure (not just suggestion retry)
                            error_msg = self.task_manager.subtasks[current_task_index].get('error', '')
                            if "Execution failed" in error_msg: # Check for execution failure messages
                                 logger.warning(f"Execution failed for step {current_task_index + 1}. Triggering re-planning.")
                                 replan_reason = f"Action execution failed for step '{current_planned_task['description']}'. Error: {error_msg}"
                                 if self._trigger_re_planning(current_planned_task, replan_reason):
                                      logger.info("Re-planning successful after execution failure.")
                                      # Loop continues with recovery steps
                                 else:
                                      # Re-planning failed or declined
                                      if not self._user_abort_recording:
                                           logger.error("Re-planning failed/declined after execution error. Step remains failed.")
                                           # Task already marked as failed by _handle_interactive_step_recording
                            # else: It was likely marked failed to retry suggestion - allow normal retry flow

                        elif current_task_status == 'done' or current_task_status == 'skipped':
                             self._consecutive_suggestion_failures = 0 # Reset failure counter on success/skip

                    else: # Should not happen
                        logger.error(f"Unexpected AI suggestion action: {ai_suggestion.get('action')}. Skipping step.")
                        self.task_manager.update_subtask_status(current_task_index, "failed", error="Unexpected AI action suggestion")


                # --- Cleanup after processing a step attempt ---
                self.browser_controller.clear_highlights()
                # Listener removal is handled within _handle_interactive_step_recording and wait_for_user_click...
                # self.browser_controller.remove_click_listener() # Ensure listener is off - redundant?

                # Small delay between steps/attempts
                if not self._user_abort_recording: # Don't delay if aborting
                     time.sleep(0.3)


            # --- Loop End ---
            if not recording_status["success"] and iteration_count >= MAX_RECORDING_ITERATIONS:
                 recording_status["message"] = f"⚠️ Recording Stopped: Maximum iterations ({MAX_RECORDING_ITERATIONS}) reached."
                 recording_status["success"] = False # Ensure max iterations means failure
                 logger.warning(recording_status["message"])

            # --- Final Save ---
            if not self._user_abort_recording and self.recorded_steps:
                try:
                    if recording_status.get("success", False): # Only check if currently marked as success
                         perm_failed_tasks_final = [t for t in self.task_manager.subtasks if t['status'] == 'failed' and t['attempts'] > self.task_manager.max_retries_per_subtask]
                         if perm_failed_tasks_final:
                              recording_status["success"] = False # Override success if any task failed
                              recording_status["message"] = recording_status["message"].replace("completed.", "completed with failures.") # Adjust message
                              logger.warning("Overriding overall success status to False due to permanently failed steps found.")
                    output_data = {
                        "test_name": f"{feature_description[:50]}_Test",
                        "feature_description": feature_description,
                        "recorded_at": datetime.utcnow().isoformat() + "Z",
                        "steps": self.recorded_steps
                    }
                    ts = time.strftime("%Y%m%d_%H%M%S")
                    safe_feature_name = re.sub(r'[^\w\-]+', '_', feature_description)[:50]
                    filename = f"test_{safe_feature_name}_{ts}.json"
                    output_dir = "output"
                    if not os.path.exists(output_dir):
                        os.makedirs(output_dir)
                    self.output_file_path = os.path.join(output_dir, filename)

                    with open(self.output_file_path, 'w', encoding='utf-8') as f:
                        json.dump(output_data, f, indent=2, ensure_ascii=False)

                    recording_status["output_file"] = self.output_file_path
                    recording_status["steps_recorded"] = len(self.recorded_steps)
                    # Set success only if we saved something and didn't explicitly fail/abort
                    if recording_status["success"]:
                         logger.info(f"Recording successfully saved to: {self.output_file_path}")
                    else:
                         logger.warning(f"Recording finished with status: {'Failed' if not self._user_abort_recording else 'Aborted'}. Saved {len(self.recorded_steps)} steps to: {self.output_file_path}. Message: {recording_status.get('message')}")


                except Exception as save_e:
                    logger.error(f"Failed to save recorded steps to JSON: {save_e}", exc_info=True)
                    recording_status["message"] = f"Failed to save recording: {save_e}"
                    recording_status["success"] = False
            elif self._user_abort_recording:
                 recording_status["message"] = "Recording aborted by user. No file saved."
                 recording_status["success"] = False
            else: # No steps recorded
                 recording_status["message"] = "No steps were recorded."
                 recording_status["success"] = False

        except ValueError as e: # Catch planning errors specifically
             logger.critical(f"Test planning failed: {e}", exc_info=True)
             recording_status["message"] = f"❌ Test Planning Failed: {e}"
             recording_status["success"] = False # Ensure failure state
        except Exception as e:
            logger.critical(f"An critical unexpected error occurred during recording: {e}", exc_info=True)
            recording_status["message"] = f"❌ Critical Error during recording: {e}"
            recording_status["success"] = False # Ensure failure state
        finally:
            logger.info("--- Ending Test Recording ---")
            # Ensure cleanup even if browser wasn't started fully
            if hasattr(self, 'browser_controller') and self.browser_controller:
                 self.browser_controller.clear_highlights()
                 self.browser_controller.remove_click_listener() # Attempt removal
                 self.browser_controller.close()

            end_time = time.time()
            recording_status["duration_seconds"] = round(end_time - start_time, 2)
            logger.info(f"Recording process finished in {recording_status['duration_seconds']:.2f} seconds.")
            logger.info(f"Final Recording Status: {'Success' if recording_status['success'] else 'Failed/Aborted'} - {recording_status['message']}")
            if recording_status.get("output_file"):
                 logger.info(f"Output file: {recording_status.get('output_file')}")

        return recording_status # Return the detailed status dictionary

    # --- Legacy run method (can be removed or kept for non-recorder execution if needed) ---
    # def run(self, feature_description: str) -> Dict[str, Any]:
    #    if self.is_recorder_mode:
    #         logger.error("Cannot run legacy execution method in recorder mode. Use record() instead.")
    #         return {"status": "FAIL", "message": "Wrong mode"}
    #     # ... (Original run method logic would go here) ...
    #     logger.warning("Legacy run method executed.")
    #     # This part needs significant review if kept, as many helper methods were changed.
    #     # For now, assume it's deprecated by the Recorder/Executor model.
    #     return {"status": "FAIL", "message": "Legacy run method not fully supported anymore."}