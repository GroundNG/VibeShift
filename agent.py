# agent.py
import json
import logging
import time
import re
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
from typing import Dict, Any, Optional, List, Tuple
import random
import os

# Use relative imports within the package
from browser_controller import BrowserController
from llm_client import GeminiClient
from vision_processor import VisionProcessor
from task_manager import TaskManager
from dom.views import DOMState, DOMElementNode, SelectorMap # Import DOM types

# Configure logger
logger = logging.getLogger(__name__)

# --- Debugging Settings ---
# Reduce delay for faster testing, increase if steps seem too fast for the page
ARTIFICIAL_DELAY_SECS = 1.0
# --- End Debugging Settings ---

class WebAgent:
    """
    Orchestrates automated web testing using structured DOM analysis, Playwright,
    Gemini (text and vision), and task management focused on test execution and verification.
    """

    def __init__(self,
                 gemini_client: GeminiClient,
                 headless: bool = True,
                 max_iterations: int = 30, # Max iterations per test run
                 max_history_length: int = 8,
                 max_retries_per_subtask: int = 2, # Max retries per *failed* step
                 max_extracted_data_history: int = 7): # History for verification

        self.gemini_client = gemini_client
        self.browser_controller = BrowserController(headless=headless)
        self.vision_processor = VisionProcessor(gemini_client)
        # TaskManager now manages test steps
        self.task_manager = TaskManager(max_retries_per_subtask=max_retries_per_subtask)
        self.history: List[Dict[str, Any]] = []
        self.extracted_data_history: List[Dict[str, Any]] = []
        self.max_iterations = max_iterations
        self.max_history_length = max_history_length
        self.max_extracted_data_history = max_extracted_data_history
        self.output_file_path: Optional[str] = None # For potential evidence saving
        self.feature_description: Optional[str] = None # Store the feature being tested
        # Store the latest DOM state for use in action execution
        self._latest_dom_state: Optional[DOMState] = None

        logger.info(f"WebAgent (Test Automation Mode) initialized (headless={headless}, max_iter={max_iterations}, max_hist={max_history_length}, max_retries={max_retries_per_subtask}, max_extract_hist={max_extracted_data_history}).")
        logger.debug(f"[DEBUG_AGENT] Artificial delay between steps set to: {ARTIFICIAL_DELAY_SECS}s")


    def _add_to_history(self, entry_type: str, data: Any):
        """Adds an entry to the agent's history, maintaining max length."""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_data_str = "..."
        try:
            # Basic sanitization
            if isinstance(data, dict):
                log_data = {k: (str(v)[:200] + '...' if len(str(v)) > 200 else v)
                             for k, v in data.items()}
            elif isinstance(data, (str, bytes)): # Handle bytes too
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
        if not self.history: return "No history yet."
        summary = "Recent History (Oldest First):\n"
        for entry in self.history:
            # Avoid serializing potentially large data again
            entry_data_str = str(entry['data'])
            if len(entry_data_str) > 300: entry_data_str = entry_data_str[:297] + "..."
            summary += f"- [{entry['type']}] {entry_data_str}\n"
        return summary.strip()


    def _clean_llm_response_to_json(self, llm_output: str) -> Optional[Dict[str, Any]]:
        """
        Attempts to extract and parse JSON from the LLM's potentially messy output,
        handling common issues like unescaped quotes within string values (esp. selectors).
        """
        logger.debug(f"[LLM PARSE] Attempting to parse LLM response (length: {len(llm_output)}).")

        # 1. Extract JSON block (markdown or first { to last })
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", llm_output, re.DOTALL | re.IGNORECASE)
        if match:
            json_str = match.group(1).strip()
            logger.debug(f"[LLM PARSE] Extracted JSON from markdown block: {json_str[:500]}...")
        else:
            start_index = llm_output.find('{')
            end_index = llm_output.rfind('}')
            if start_index != -1 and end_index != -1 and end_index > start_index:
                json_str = llm_output[start_index : end_index + 1].strip()
                logger.debug(f"[LLM PARSE] Attempting to parse extracted JSON between {{ and }}: {json_str[:500]}...")
            else:
                 logger.warning("[LLM PARSE] Could not find JSON structure (markdown block or curly braces) in LLM output.")
                 self._add_to_history("LLM Parse Error", {"reason": "No JSON structure found", "raw_output_snippet": llm_output[:200]})
                 return None

        # 2. Pre-processing: Fix common LLM errors (like unescaped quotes in values)
        try:
            # Helper to escape quotes within matched values for specific keys
            def escape_quotes_replacer(match):
                key_part = match.group(1) # "selector" or "text" or "reasoning" etc.
                colon_part = match.group(2) # :\s*
                open_quote = match.group(3) # "
                value = match.group(4)      # The actual value string
                close_quote = match.group(5) # "
                # Escape internal quotes *unless* they are already escaped
                escaped_value = re.sub(r'(?<!\\)"', r'\\"', value)
                return f'{key_part}{colon_part}{open_quote}{escaped_value}{close_quote}' # Rebuild

            # Apply the replacer to keys known to contain potentially problematic strings
            keys_to_escape = ["selector", "text", "reasoning", "url", "result", "answer", "reason", "file_path"]
            pattern_str = r'(\"(?:' + '|'.join(keys_to_escape) + r')\")(\s*:\s*)(\")(.*?)(\")'
            pattern = re.compile(pattern_str, re.DOTALL)
            json_str = pattern.sub(escape_quotes_replacer, json_str)
            # logger.debug(f"[LLM PARSE] JSON string after attempting quote escaping: {json_str[:500]}...")

            # --- Fix common escape sequences ---
            # Do this *after* fixing internal quotes
            json_str = json_str.replace('\\\\n', '\\n').replace('\\n', '\n') # Handle escaped newlines carefully
            json_str = json_str.replace('\\\\"', '\\"') # Handle already escaped quotes correctly
            json_str = json_str.replace('\\\\t', '\\t') # Handle tabs
            # Remove trailing commas before closing braces/brackets
            json_str = re.sub(r',\s*([\}\]])', r'\1', json_str)
            # logger.debug(f"[LLM PARSE] JSON string after escape sequence cleaning: {json_str[:500]}...")

        except Exception as clean_e:
             logger.warning(f"[LLM PARSE] Error during pre-parsing cleaning: {clean_e}")
             # Continue attempt to parse anyway

        # 3. Attempt Parsing
        try:
            parsed_json = json.loads(json_str)
            if isinstance(parsed_json, dict) and "action" in parsed_json and "parameters" in parsed_json:
                logger.debug(f"[LLM PARSE] Successfully parsed action JSON: {parsed_json}")
                return parsed_json
            else:
                 logger.warning(f"[LLM PARSE] Parsed JSON missing required keys ('action', 'parameters') or is not a dict: {parsed_json}")
                 self._add_to_history("LLM Parse Error", {"reason": "Missing required keys", "parsed_json": parsed_json, "cleaned_json_string": json_str[:200]})
                 return None
        except json.JSONDecodeError as e:
            logger.error(f"[LLM PARSE] Failed to decode JSON from LLM output: {e}")
            logger.error(f"[LLM PARSE] Faulty JSON string snippet (around pos {e.pos}): {json_str[max(0, e.pos-50):e.pos+50]}")
            # logger.debug(f"[LLM PARSE] Full cleaned JSON string passed to loads:\n{json_str}") # Uncomment for deep debug
            self._add_to_history("LLM Parse Error", {"reason": f"JSONDecodeError: {e}", "error_pos": e.pos, "json_string_snippet": json_str[max(0, e.pos-50):e.pos+50]})
            return None
        except Exception as e:
             logger.error(f"[LLM PARSE] Unexpected error during final JSON parsing: {e}", exc_info=True)
             return None


    def _plan_subtasks(self, feature_description: str):
        """Uses the LLM to break down the feature test into sequential, verifiable steps."""
        logger.info(f"Planning test steps for feature: '{feature_description}'")
        self.feature_description = feature_description # Store for context
        prompt = f"""
        You are an AI Test Engineer. Given the feature description for testing: "{feature_description}"

        Break this down into a sequence of specific, verifiable browser interaction steps that an automated web agent can perform.
        Each step must be a single, clear instruction or verification (e.g., "Navigate to...", "Click the button [index] with text 'Submit'", "Extract text from element [index] with id 'price'").
        The agent will be provided with a list of interactive elements identified by index, like `[index]<tag attributes...>text</>`. Plan steps assuming the agent uses these indices for interaction.

        **Key Elements of Test Steps:**
        1.  **Navigation:** `Navigate to https://example.com/login`
        2.  **Action:** `Click element [12] (Submit Button)` or `Type 'testuser' into element [5] (Username Input)`
        3.  **Verification:** Phrase steps clearly indicating *what* to verify.
            - `Verify text "Login Successful" is present in element [25] (Status Message)`
            - `Verify attribute 'href' of element [3] (Profile Link) contains '/profile'`
            - `Extract text from element [10] (Product Price)` (Agent will store this, subsequent analysis determines pass/fail if needed)
        4.  **Scrolling:** `Scroll down` (if needed, typically before locating an element suspected off-screen).

        **Output ONLY the test steps as a JSON list of strings.** No explanations or markdown outside the list.

        Example Test Case: "Test login on example.com with user 'tester' and pass 'pwd123', then verify the welcome message contains 'tester'."
        Example JSON Output:
        ```json
        [
          "Navigate to https://example.com/login",
          "Type 'tester' into element [index_of_username_input]",
          "Type 'pwd123' into element [index_of_password_input]",
          "Click element [index_of_login_button]",
          "Extract text from element [index_of_welcome_message]",
          "Verify extracted text from previous step contains 'tester'"
        ]
        ```
        *(Note: The agent needs to understand how to find the correct indices dynamically based on the descriptions)*

        Now, generate the JSON list of test steps for the feature: "{feature_description}"
        JSON Test Step List:
        ```json
        """
        logger.debug(f"[TEST PLAN] Sending Subtask Planning Prompt:\n{prompt[:500]}...")
        response = self.gemini_client.generate_text(prompt)
        logger.debug(f"[TEST PLAN] LLM RAW response:\n{response[:500]}...")

        subtasks = None
        try:
             match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", response, re.DOTALL | re.IGNORECASE)
             if match:
                 json_str = match.group(1).strip()
                 logger.debug("[TEST PLAN] Extracted JSON list from markdown block for test steps.")
             else:
                  stripped_response = response.strip()
                  if stripped_response.startswith('[') and stripped_response.endswith(']'):
                       json_str = stripped_response
                       logger.debug("[TEST PLAN] Attempting to parse entire response as JSON list.")
                  else:
                       json_str = None
                       logger.warning("[TEST PLAN] Could not find JSON list in markdown or as full response.")

             if json_str:
                  # Attempt to fix potential trailing commas before parsing
                  json_str_fixed = re.sub(r',\s*\]', ']', json_str)
                  json_str_fixed = re.sub(r',\s*\}', '}', json_str_fixed)
                  parsed_list = json.loads(json_str_fixed)
                  if isinstance(parsed_list, list) and all(isinstance(s, str) and s for s in parsed_list):
                      subtasks = parsed_list
                  else:
                       logger.warning(f"[TEST PLAN] Parsed JSON is not a list of non-empty strings: {parsed_list}")

        except json.JSONDecodeError as e:
             logger.error(f"[TEST PLAN] Failed to decode JSON subtask list: {e}")
             logger.debug(f"[TEST PLAN] Faulty JSON string for planning: {json_str}")
        except Exception as e:
            logger.error(f"[TEST PLAN] An unexpected error occurred during subtask parsing: {e}", exc_info=True)

        if subtasks and len(subtasks) > 0:
            self.task_manager.add_subtasks(subtasks) # TaskManager now stores test steps
            self._add_to_history("Test Plan Created", {"feature": feature_description, "steps": subtasks})
            logger.info(f"Successfully planned {len(subtasks)} test steps.")
            logger.debug(f"[TEST PLAN] Planned Steps: {subtasks}")
        else:
            logger.error("[TEST PLAN] Failed to generate or parse valid test steps from LLM response.")
            self._add_to_history("Test Plan Failed", {"feature": feature_description, "raw_response": response[:500]+"..."})
            # For testing, failing to plan is a critical failure.
            raise ValueError("Failed to generate a valid test plan from the feature description.")


    def _get_extracted_data_summary(self) -> str:
        """Provides a concise summary of recently extracted data for the LLM (useful for verification steps)."""
        if not self.extracted_data_history:
            return "No data extracted yet."

        summary = "Recently Extracted Data (Most Recent First - useful for verification):\n"
        start_index = max(0, len(self.extracted_data_history) - self.max_extracted_data_history)
        for entry in reversed(self.extracted_data_history[start_index:]):
             data_snippet = str(entry.get('data', ''))
             if len(data_snippet) > 150: data_snippet = data_snippet[:147] + "..."
             step_desc_snippet = entry.get('subtask_desc', 'N/A')[:50] + ('...' if len(entry.get('subtask_desc', 'N/A')) > 50 else '')
             # Include index and potentially selector for context
             index_info = f"Index:[{entry.get('index', '?')}]"
             selector_info = f" Sel:'{entry.get('selector', '')[:30]}...'" if entry.get('selector') else ""
             summary += f"- Step {entry.get('subtask_index', '?')+1} ('{step_desc_snippet}'): {index_info}{selector_info} Type={entry.get('type')}, Data={data_snippet}\n"
        return summary.strip()


    def _determine_next_action(self,
                               current_task: Dict[str, Any],
                               current_url: str,
                               dom_context_str: str, # Changed from cleaned_html
                               screenshot_analysis: Optional[str] = None
                               ) -> Optional[Dict[str, Any]]:
        """Uses LLM to determine the specific browser action for the current test step based on structured DOM."""
        logger.info(f"Determining next action for test step: '{current_task['description']}'")

        prompt = f"""
You are an expert AI web testing agent controller. Your goal is to decide the *single next browser action* to execute the current test step, using the provided interactive element context.

**Feature Under Test:** {self.feature_description}
**Current Test Step:** {current_task['description']}
**Current URL:** {current_url}
**Test Progress:** Attempt {current_task['attempts']} of {self.task_manager.max_retries_per_subtask + 1}.

**Input Context:**

**1. Interactive Element Context (Key Elements):**
This is the structured view of interactive elements detected. Use the `[index]` number to specify the target element.
```html
{dom_context_str}
```

**Available Actions (Output JSON Format):**
1.  `navigate`: Go to a URL.
    ```json
    {{"action": "navigate", "parameters": {{"url": "https://example.com"}}, "reasoning": "Navigate to the target page."}}
    ```
2.  `click`: Click an element identified by its index.
    ```json
    {{"action": "click", "parameters": {{"index": INDEX_NUMBER_FROM_CONTEXT}}, "reasoning": "Click the login button."}}
    ```
3.  `type`: Type text into an element identified by its index. Use exact text from test step.
    ```json
    {{"action": "type", "parameters": {{"index": INDEX_NUMBER_FROM_CONTEXT, "text": "My text"}}, "reasoning": "Enter username."}}
    ```
4.  `scroll`: Scroll the page up or down (if target element might be off-screen).
    ```json
    {{"action": "scroll", "parameters": {{"direction": "down" | "up"}}, "reasoning": "Scroll down to find the 'Next' button."}}
    ```
5.  `extract_text`: Get *visible text* from element [index]. Used for verification. Result stored internally.
    ```json
    {{"action": "extract_text", "parameters": {{"index": INDEX_NUMBER_FROM_CONTEXT}}, "reasoning": "Extract the product title for verification."}}
    ```
6.  `extract_attributes`: Get specific *attribute values* (e.g., `href`, `value`) from element [index]. Used for verification. Result stored internally.
    ```json
    {{"action": "extract_attributes", "parameters": {{"index": INDEX_NUMBER_FROM_CONTEXT, "attributes": ["href", "title"]}}, "reasoning": "Extract the link URL for verification."}}
    ```
7.  `save_json`: (Rare in testing) Save collected data as evidence.
    ```json
    {{ "action": "save_json", "parameters": {{ "data": [...], "file_path": "test_evidence.json" }}, "reasoning": "Save extracted details as evidence." }}
    ```
8.  `subtask_complete`: Current test step finished successfully (e.g., after navigation, click, type). Also use after `extract_text`/`extract_attributes` if the extraction itself succeeded (verification happens based on history).
    ```json
    {{"action": "subtask_complete", "parameters": {{"result": "Successfully typed username."}}, "reasoning": "Username entered correctly."}}
    ```
9.  `final_answer`: (Very rare in testing) Test result is a simple text answer.
    ```json
    {{"action": "final_answer", "parameters": {{"answer": "Site health check passed."}}, "reasoning": "Basic availability confirmed."}}
    ```
10. `subtask_failed`: Current test step **cannot be completed** OR **a verification failed**. Explain why clearly.
     ```json
     {{"action": "subtask_failed", "parameters": {{"reason": "Element matching 'Login Button' description not found in context."}}, "reasoning": "Cannot find necessary element."}}
     ```
     ```json
     {{"action": "subtask_failed", "parameters": {{"reason": "Verification failed: Expected text containing 'Welcome' but found 'Error' in extracted data (see history)."}}, "reasoning": "Login failed based on verified text."}}
     ```

**Further Context:**

**2. Recent Action History:**
{self._get_history_summary()}
"""
        # Add error context if retrying
        if current_task['status'] == 'in_progress' and current_task['attempts'] > 1 and current_task.get('error'):
            error_context = str(current_task['error'])
            if len(error_context) > 300: error_context = error_context[:297] + "..."
            prompt += f"\n**Previous Attempt Error:**\nAttempt {current_task['attempts'] - 1} failed: {error_context}\nConsider this error. Was the wrong index chosen? Is scrolling needed? Did the page not update?\n"

        # Add screenshot analysis if available
        if screenshot_analysis:
            analysis_snippet = screenshot_analysis[:997] + "..." if len(screenshot_analysis) > 1000 else screenshot_analysis
            prompt += f"\n**3. Screenshot Analysis (Visual Context):**\n{analysis_snippet}\n*Use this visual context to help interpret the Element Context above and choose the correct index.*\n"
        else:
            prompt += "\n**3. Screenshot Analysis:** Not available for this step.\n"

        # Add extracted data history for verification steps
        prompt += f"\n**4. Recently Extracted Data (CRITICAL for Verification Steps):**\n{self._get_extracted_data_summary()}\n**If the current step is a verification (e.g., 'Verify text...'), check if the relevant data in this history matches the expectation. If not, use `subtask_failed`.**\n"

        # Final instruction
        prompt += """
**Your Decision:**
Based on the feature, test step, element context, history, errors, and extracted data, determine the **single best next action**.
- **Choose the `index` number carefully from the Interactive Element Context** for actions targeting specific elements. Match the element description in the step to the context.
- If the required element isn't listed, consider scrolling (`scroll`) or failing the step (`subtask_failed`).
- If the step requires verification, check the extracted data history. If it fails the check, use `subtask_failed` with a clear reason.
- If extraction succeeds but verification is based on it, use `subtask_complete` for the extraction step.

Output your decision strictly as a JSON object with reasoning related to the test step execution/verification.
```json
"""
        # --- End Prompt Construction ---

        logger.debug(f"[LLM PROMPT] Sending prompt snippet for action determination:\n{prompt[:500]}...")

        # Add Delay Before LLM Call
        if ARTIFICIAL_DELAY_SECS > 0:
            logger.debug(f"[DEBUG_AGENT] Pausing for {ARTIFICIAL_DELAY_SECS}s before calling LLM...")
            time.sleep(ARTIFICIAL_DELAY_SECS)

        response = self.gemini_client.generate_text(prompt)
        logger.debug(f"[LLM RESPONSE] Raw response snippet:\n{response[:500]}...")

        action_json = self._clean_llm_response_to_json(response)

        if action_json:
             reasoning = action_json.get("reasoning", "No reasoning provided.")
             decision_summary = {"action": action_json.get("action"), "parameters": action_json.get("parameters"), "reasoning": reasoning}
             logger.info(f"[LLM Decision] Action: {decision_summary['action']}, Params: {decision_summary['parameters']}, Reasoning: {reasoning[:150]}...")
             self._add_to_history("LLM Decision", decision_summary)
             return action_json
        else:
             self._add_to_history("LLM Decision Failed", {"raw_response_snippet": response[:500]+"..."})
             logger.error("[LLM Decision Failed] Failed to get a valid JSON action from LLM.")
             return None

    def _get_current_task_index(self, current_task_desc: str) -> int:
            """Finds the index of the test step currently being processed."""
            # Find the first task matching the description that is 'in_progress'
            for i, task in enumerate(self.task_manager.subtasks):
                if task["description"] == current_task_desc and task["status"] == "in_progress":
                    # logger.debug(f"[DEBUG_AGENT] Found active task index {i} for description: '{current_task_desc[:50]}...'")
                    return i

            # Fallback: If somehow the TaskManager's index points to the right description
            if 0 <= self.task_manager.current_subtask_index < len(self.task_manager.subtasks):
                task = self.task_manager.subtasks[self.task_manager.current_subtask_index]
                if task["description"] == current_task_desc:
                    logger.warning(f"[DEBUG_AGENT] Found task index {self.task_manager.current_subtask_index} via fallback for description: '{current_task_desc[:50]}...' (Status was {task['status']})")
                    return self.task_manager.current_subtask_index

            logger.error(f"[CRITICAL] Could not determine index for active task: '{current_task_desc}'. Returning -1.")
            return -1

    def _execute_action(self, action_details: Dict[str, Any], current_task_info: Dict[str, Any]) -> Dict[str, Any]:
        """ Executes the browser action, handling index-based element selection for testing. """
        action = action_details.get("action")
        params = action_details.get("parameters", {})
        result = {"success": False, "message": f"Action '{action}' invalid.", "data": None}
        # Get index based on current task info BEFORE potentially advancing
        current_task_index = self._get_current_task_index(current_task_info["description"])

        if not action:
            result["message"] = "No action specified."
            logger.warning(f"[ACTION_EXEC] {result['message']}")
            self._add_to_history("Action Execution Error", {"reason": result["message"]})
            return result

        logger.info(f"[ACTION_EXEC] Attempting Action: {action} | Params: {params}")
        self._add_to_history("Executing Action", {"action": action, "parameters": params})

        # Delay Before Execution
        if ARTIFICIAL_DELAY_SECS > 0:
            logger.debug(f"[DEBUG_AGENT] Pausing for {ARTIFICIAL_DELAY_SECS}s before executing action '{action}'...")
            time.sleep(ARTIFICIAL_DELAY_SECS)

        generated_selector: Optional[str] = None # Store the selector derived from index
        target_node: Optional[DOMElementNode] = None # Store the node itself

        try:
            target_index = params.get("index") # Check if index is provided

            # --- Element Selection Logic (if index provided) ---
            if target_index is not None:
                 if not isinstance(target_index, int) or target_index < 0:
                      raise ValueError(f"Invalid 'index' parameter: must be a non-negative integer, got {target_index}")
                 if self._latest_dom_state is None or not self._latest_dom_state.selector_map:
                      raise PlaywrightError("DOM state or selector map is missing, cannot lookup index.")

                 target_node = self._latest_dom_state.selector_map.get(target_index)
                 if target_node is None:
                      # Log available indices for debugging
                      available_indices = list(self._latest_dom_state.selector_map.keys())
                      logger.error(f"Element index [{target_index}] not found in DOM context map. Available indices: {available_indices}")
                      raise PlaywrightError(f"Element index [{target_index}] not found in the current DOM context map.")

                 # Generate selector from the node
                 generated_selector = target_node.css_selector # Check if cached
                 if not generated_selector:
                       generated_selector = self.browser_controller.get_selector_for_node(target_node)
                       if generated_selector:
                            target_node.css_selector = generated_selector # Cache it
                       else:
                            raise PlaywrightError(f"Failed to generate selector for element index [{target_index}] (XPath: {target_node.xpath}).")

                 logger.info(f"Resolved index [{target_index}] to element: {target_node.tag_name} using selector: {generated_selector}")

            # --- Action Execution ---
            if action == "navigate":
                url = params.get("url")
                if not url or not isinstance(url, str): raise ValueError("Missing or invalid 'url'.")
                self.browser_controller.goto(url)
                result["success"] = True
                result["message"] = f"Navigated to {url}."

            elif action == "click":
                if generated_selector is None: raise ValueError("Missing 'index' parameter for click action.")
                self.browser_controller.click(generated_selector)
                result["success"] = True
                result["message"] = f"Clicked element index [{target_index}] (selector: {generated_selector})."

            elif action == "type":
                text = params.get("text")
                if generated_selector is None: raise ValueError("Missing 'index' parameter for type action.")
                if text is None or not isinstance(text, str): raise ValueError("Missing or invalid 'text'.")
                self.browser_controller.type(generated_selector, text)
                result["success"] = True
                result["message"] = f"Typed into element index [{target_index}] (selector: {generated_selector})."

            elif action == "scroll":
                direction = params.get("direction")
                if direction not in ["up", "down"]: raise ValueError("Invalid 'direction' (must be 'up' or 'down').")
                self.browser_controller.scroll(direction)
                result["success"] = True
                result["message"] = f"Scrolled {direction}."

            elif action == "extract_text":
                if generated_selector is None: raise ValueError("Missing 'index' parameter for extract_text.")
                extracted_data = self.browser_controller.extract_text(generated_selector)
                if isinstance(extracted_data, str) and extracted_data.startswith("Error:"):
                     raise PlaywrightError(f"Extraction failed: {extracted_data}")
                result["success"] = True
                result["message"] = f"Extracted text from index [{target_index}] (selector: {generated_selector})."
                result["data"] = extracted_data
                # Store in history
                if current_task_index != -1 and target_node:
                     self.extracted_data_history.append({
                         "subtask_index": current_task_index, "subtask_desc": current_task_info["description"],
                         "type": "text", "data": extracted_data, "index": target_index,
                         "selector": generated_selector, "tag": target_node.tag_name
                     })
                     logger.info(f"Stored extracted text from step {current_task_index + 1} for verification.")
                else: logger.warning("Could not determine step index or node for storing extracted text.")

            elif action == "extract_attributes":
                attributes = params.get("attributes")
                if generated_selector is None: raise ValueError("Missing 'index' parameter for extract_attributes.")
                if not attributes or not isinstance(attributes, list) or not all(isinstance(a, str) for a in attributes):
                    raise ValueError("Missing or invalid 'attributes' list.")
                extracted_data_dict = self.browser_controller.extract_attributes(generated_selector, attributes)
                if extracted_data_dict.get("error"):
                    raise PlaywrightError(f"Attribute extraction failed: {extracted_data_dict['error']}")
                result["success"] = True
                result["message"] = f"Extracted attributes {attributes} from index [{target_index}] (selector: {generated_selector})."
                result["data"] = extracted_data_dict
                 # Store in history
                if current_task_index != -1 and target_node:
                     self.extracted_data_history.append({
                         "subtask_index": current_task_index, "subtask_desc": current_task_info["description"],
                         "type": "attributes", "data": extracted_data_dict, "index": target_index,
                         "selector": generated_selector, "tag": target_node.tag_name, "attributes_queried": attributes
                     })
                     logger.info(f"Stored extracted attributes from step {current_task_index + 1} for verification.")
                else: logger.warning("Could not determine step index or node for storing extracted attributes.")

            elif action == "save_json": # For evidence
                data_to_save = params.get("data")
                file_path = params.get("file_path", f"output/test_evidence_{time.strftime('%Y%m%d_%H%M%S')}.json") # Default path
                if data_to_save is None: raise ValueError("Missing 'data' for save_json.")
                if not isinstance(file_path, str) or ".." in file_path: raise ValueError("Invalid 'file_path'.")
                if not os.path.isabs(file_path) and not file_path.startswith("output/"):
                     file_path = os.path.join("output", os.path.basename(file_path))
                save_result = self.browser_controller.save_json_data(data_to_save, file_path)
                result["success"] = save_result["success"]
                result["message"] = save_result["message"]
                if save_result["success"]:
                    result["data"] = {"file_path": save_result.get("file_path")}
                    self.output_file_path = save_result.get("file_path") # Store path if saved

            elif action == "final_answer": # Rare
                answer = params.get("answer", "Test condition met.")
                result["success"] = True
                result["message"] = "Final answer determined."
                result["data"] = answer
                logger.info(f"[ACTION_EXEC] Final Answer: {str(answer)[:500]}...")

            elif action == "subtask_complete": # Step succeeded
                 subtask_result = params.get("result", "Step completed successfully.")
                 result["success"] = True
                 result["message"] = "Test step marked as complete by LLM."
                 result["data"] = subtask_result
                 logger.info(f"[ACTION_EXEC] Step Complete. Result: {str(subtask_result)[:500]}...")

            elif action == "subtask_failed": # Step failed (verification or execution impossible)
                 reason = params.get("reason", "LLM determined step failure.")
                 result["success"] = False # Explicit failure
                 result["message"] = f"Test Step Failed (LLM Decision): {reason}"
                 logger.warning(f"[ACTION_EXEC] {result['message']}")

            else:
                result["message"] = f"Unknown action: {action}"
                logger.error(f"[ACTION_EXEC] {result['message']}")

        # --- Error Handling ---
        except (PlaywrightError, PlaywrightTimeoutError) as e:
            error_msg = f"Playwright Action '{action}' Failed: {type(e).__name__}: {str(e)}"
            logger.error(f"[ACTION_EXEC] {error_msg}", exc_info=False)
            result["message"] = error_msg
            result["success"] = False
            # Store the selector that failed, if applicable and if we have the task index
            if generated_selector and target_node and current_task_index != -1 and current_task_index < len(self.task_manager.subtasks):
                 try:
                     self.task_manager.subtasks[current_task_index]['last_failed_selector'] = generated_selector
                 except IndexError:
                     logger.error(f"IndexError trying to record failed selector for index {current_task_index}")

        except ValueError as e: # Catch bad parameters, invalid index, selector generation failure
            error_msg = f"Action '{action}' Input/Setup Error: {e}"
            logger.error(f"[ACTION_EXEC] {error_msg}")
            result["message"] = error_msg
            result["success"] = False
        except Exception as e:
            error_msg = f"Unexpected Error during action '{action}': {type(e).__name__}: {e}"
            logger.critical(f"[ACTION_EXEC] {error_msg}", exc_info=True)
            result["message"] = error_msg
            result["success"] = False

        # Log Action Result
        log_level = logging.INFO if result["success"] else logging.WARNING
        logger.log(log_level, f"[ACTION_RESULT] Action '{action}' | Success: {result['success']} | Message: {result['message']}")
        # Avoid double-logging extracted data
        if result.get('data') and action not in ["extract_text", "extract_attributes"]:
             logger.debug(f"[ACTION_RESULT] Data snippet: {str(result.get('data'))[:100]}...")
        self._add_to_history("Action Result", {"success": result["success"], "message": result["message"], "data_snippet": str(result.get('data'))[:100]+"..." if result.get('data') else None})

        # Delay After Execution
        if ARTIFICIAL_DELAY_SECS > 0:
            logger.debug(f"[DEBUG_AGENT] Pausing for {ARTIFICIAL_DELAY_SECS}s after executing action '{action}'...")
            time.sleep(ARTIFICIAL_DELAY_SECS)

        return result


    def run(self, feature_description: str) -> Dict[str, Any]:
        """
        Runs the automated test for the given feature description.
        Captures and reports console messages on failure.
        """
        logger.info(f"--- Starting Test Run --- Feature: {feature_description}")
        print(f"\n--- Starting Test Run for Feature ---\n{feature_description}\n" + "-"*35)
        start_time = time.time()
        # Initialize run status, default to FAIL
        run_status = {
            "status": "FAIL",
            "feature": feature_description,
            "message": "Test run initiated.",
            "failed_step_index": None,
            "failed_step_description": None,
            "error_details": None,
            "output_file": None, # Path to saved evidence, if any
            "screenshot_on_failure": None, # Path to failure screenshot
            "console_messages_on_failure": [], # Relevant errors/warnings on failure
            "all_console_messages": [], # All captured messages for reporting
            "duration_seconds": 0.0,
            "steps_summary": "Not started.",
            "total_steps": 0,
            "steps_completed": 0, # Number of steps successfully marked 'done'
        }
        self.history = []
        self.extracted_data_history = []
        self.output_file_path = None
        self._latest_dom_state = None # Reset DOM state

        try:
            logger.debug("[AGENT] Starting browser controller...")
            self.browser_controller.start() # This now also inits DomService
            # Assuming BrowserController has these methods:
            try:
                self.browser_controller.clear_console_messages()
            except AttributeError:
                logger.warning("BrowserController does not have 'clear_console_messages' method. Skipping.")


            self.task_manager.set_main_task(feature_description)
            logger.debug("[AGENT] Planning test steps...")
            self._plan_subtasks(feature_description) # Can raise ValueError if planning fails

            if not self.task_manager.subtasks:
                 # This case should be covered by _plan_subtasks raising error now
                 run_status["message"] = "❌ Test Planning Failed: No steps generated."
                 run_status["error_details"] = "Subtask planning returned empty list."
                 raise Exception(run_status["message"]) # Stop execution

            run_status["total_steps"] = len(self.task_manager.subtasks)
            iteration_count = 0
            # Use max_iterations setting for the test run limit
            max_iterations_for_test = self.max_iterations

            while iteration_count < max_iterations_for_test:
                iteration_count += 1
                logger.info(f"\n===== Test Iteration {iteration_count}/{max_iterations_for_test} =====")
                print(f"\nIteration {iteration_count}/{max_iterations_for_test}...")

                current_task = self.task_manager.get_next_subtask()

                if not current_task:
                    # Loop ended, check final status
                    if self.task_manager.is_complete():
                         all_done = all(t['status'] == 'done' for t in self.task_manager.subtasks)
                         if all_done:
                              run_status["status"] = "PASS"
                              run_status["message"] = "✅ Test Passed: All steps executed successfully."
                              run_status["steps_completed"] = len(self.task_manager.subtasks)
                              logger.info(run_status["message"])
                         else:
                              logger.warning("[TEST RESULT] Test finished, but not all steps were 'done'. Determining failure point.")
                              # Find first non-done task to report failure
                              for idx, task in enumerate(self.task_manager.subtasks):
                                   if task['status'] != 'done':
                                       run_status["status"] = "FAIL" # Ensure fail status
                                       run_status["message"] = f"❌ Test Failed: Step {idx + 1} did not complete successfully (Status: {task['status']})."
                                       run_status["failed_step_index"] = idx
                                       run_status["failed_step_description"] = task['description']
                                       run_status["error_details"] = task.get('error', 'Step ended with non-done status.')
                                       logger.error(run_status["message"])
                                       break # Report first failure
                    else: # Should not happen if is_complete is correct
                         run_status["message"] = "❌ Test Error: Step loop ended unexpectedly (not complete)."
                         run_status["error_details"] = "TaskManager inconsistent state."
                         logger.error(run_status["message"])
                    break # Exit loop

                # Get index AFTER get_next_subtask marks it as in_progress
                current_task_index = self._get_current_task_index(current_task["description"])
                if current_task_index == -1: # Should be impossible if get_next_subtask worked
                     run_status["message"] = f"❌ Critical Error: Could not determine index for current step: {current_task['description']}. Aborting."
                     run_status["error_details"] = "Internal state error tracking current step index."
                     logger.critical(run_status["message"])
                     break

                # Track completed steps up to the one *before* the current one
                run_status["steps_completed"] = current_task_index

                logger.info(f"Current Test Step #{current_task_index + 1}/{run_status['total_steps']} (Attempt {current_task['attempts']}): {current_task['description']}")
                print(f"Executing Step {current_task_index + 1}: {current_task['description']} (Attempt {current_task['attempts']})")

                # --- State Gathering ---
                logger.info("Gathering browser state and structured DOM...")
                current_url = "Error: Could not get URL"
                dom_context_str = "Error: Could not process DOM"
                screenshot_analysis = None
                self._latest_dom_state = None # Clear previous state

                try:
                    current_url = self.browser_controller.get_current_url()
                    logger.debug(f"[AGENT] Current URL: {current_url}")

                    if not current_url.startswith("Error"):
                         # Get structured DOM, highlight if not headless
                         should_highlight = not self.browser_controller.headless
                         logger.debug(f"[AGENT] Requesting structured DOM (highlight={should_highlight})...")
                         self._latest_dom_state = self.browser_controller.get_structured_dom(
                             highlight_elements=should_highlight,
                             viewport_expansion=0 # Focus on viewport
                         )

                         if self._latest_dom_state and self._latest_dom_state.element_tree:
                              # Generate string for LLM
                              dom_context_str = self._latest_dom_state.element_tree.clickable_elements_to_string(
                                   include_attributes=['id', 'name', 'class', 'aria-label', 'placeholder', 'role', 'type', 'value', 'title', 'href'] # Include href
                              )
                              logger.debug(f"[AGENT] Generated DOM context string (length: {len(dom_context_str)}).")
                         else:
                              dom_context_str = "Error processing DOM structure."
                              logger.error("[AGENT] Failed to get valid DOM state.")
                    else:
                        dom_context_str = "Error: Could not get current URL."

                    # Screenshot and analyze ONLY on retry attempts with an error
                    screenshot_bytes = self.browser_controller.take_screenshot()
                    if screenshot_bytes and current_task['attempts'] > 1 and current_task.get('error'):
                         logger.info("[AGENT] Retry attempt detected. Getting screenshot analysis...")
                         failed_selector = current_task.get('last_failed_selector')
                         error_context = current_task.get('error')
                         screenshot_analysis = self.vision_processor.analyze_screenshot_for_action(
                              screenshot_bytes, current_task['description'], failed_selector, error_context
                         )
                         analysis_snippet = screenshot_analysis[:300] + ('...' if len(screenshot_analysis)>300 else '')
                         logger.debug(f"[AGENT] Received screenshot analysis snippet:\n{analysis_snippet}")
                         self._add_to_history("Screenshot Analysis", {"analysis_snippet": analysis_snippet})
                    elif screenshot_bytes:
                         logger.debug("[AGENT] Took screenshot (not analyzing).")
                    else:
                         logger.warning("[AGENT] Failed to take screenshot.")

                except Exception as e:
                    logger.error(f"Failed to gather browser state/DOM: {e}", exc_info=True)
                    dom_context_str = f"Error gathering state: {e}"
                    current_url = "Error gathering state"
                    self._latest_dom_state = None
                    # Allow LLM to try and proceed or fail the step based on the error context

                # --- Action Determination ---
                action_decision = self._determine_next_action(
                    current_task, current_url, dom_context_str, screenshot_analysis
                )

                if not action_decision:
                    logger.error("LLM failed to provide a valid action. Marking attempt as failed.")
                    error_msg = "LLM failed to determine a valid action."
                    # Update task status for this attempt
                    self.task_manager.update_subtask_status(current_task_index, "failed", error=error_msg)
                    # Check if retries are exhausted for this step
                    # Access attempts *after* get_next_subtask incremented it
                    current_attempts = self.task_manager.subtasks[current_task_index]['attempts']
                    if current_attempts >= self.task_manager.max_retries_per_subtask:
                        logger.error(f"Step {current_task_index + 1} failed permanently due to LLM decision failure.")
                        run_status["message"] = f"❌ Test Failed: LLM could not decide action for step {current_task_index + 1} after {current_attempts} attempts."
                        run_status["failed_step_index"] = current_task_index
                        run_status["failed_step_description"] = current_task['description']
                        run_status["error_details"] = error_msg
                        run_status["status"] = "FAIL" # Ensure overall failure
                        break # Exit the loop
                    else:
                        logger.warning(f"LLM decision failed for step {current_task_index + 1}, retrying (Attempt {current_attempts+1})...")
                        time.sleep(random.uniform(1.0, 2.0))
                        continue # Proceed to next iteration for retry


                # --- Action Execution ---
                execution_result = self._execute_action(action_decision, current_task)
                action_type = action_decision.get("action")

                # --- Update Test Step Status ---
                update_result = execution_result.get("data", execution_result["message"])
                update_error = None if execution_result["success"] else execution_result["message"]
                new_status = "unknown"

                if execution_result["success"]:
                    # Handle potentially successful end states (less common for tests)
                    if action_type in ["final_answer", "save_json"]:
                         new_status = "done"
                         if action_type == "save_json":
                             run_status["output_file"] = execution_result.get("data", {}).get("file_path")
                         logger.info(f"Action '{action_type}' completed successfully for step {current_task_index + 1}.")
                         # Test doesn't necessarily pass yet, just this step is done
                    elif action_type == "subtask_complete":
                         new_status = "done"
                         logger.info(f"Step {current_task_index + 1} marked complete by LLM.")
                    else: # Other successful browser actions (navigate, click, type, extract)
                         new_status = "done" # Mark step as done
                         logger.info(f"Action '{action_type}' executed successfully for step {current_task_index + 1}.")

                else: # Execution failed OR subtask_failed action from LLM
                    error_msg = execution_result["message"]
                    logger.warning(f"Execution/Verification failed for test step #{current_task_index + 1}. Status: {action_type}. Error: {error_msg}")
                    new_status = "failed" # Mark the current attempt as failed
                    update_error = error_msg # Store the failure reason

                    # --- Immediate Failure Handling ---
                    # Check attempts *after* potential failure
                    current_attempts = self.task_manager.subtasks[current_task_index]['attempts']
                    if current_attempts >= self.task_manager.max_retries_per_subtask:
                        logger.error(f"Test step {current_task_index + 1} failed permanently after {current_attempts} attempts. Stopping test.")
                        run_status["status"] = "FAIL" # Ensure overall failure
                        run_status["message"] = f"❌ Test Failed: Step {current_task_index + 1} failed permanently."
                        run_status["failed_step_index"] = current_task_index
                        run_status["failed_step_description"] = current_task['description']
                        run_status["error_details"] = update_error
                        # Update status before breaking
                        self.task_manager.update_subtask_status(current_task_index, new_status, result=None, error=update_error)
                        break # Exit the loop immediately
                    else:
                         logger.warning(f"Test step {current_task_index + 1} failed, retrying (Attempt {current_attempts+1})...")
                         time.sleep(random.uniform(1.0, 2.0)) # Delay before retry

                # Update task status via TaskManager if loop didn't break
                if run_status["status"] != "FAIL": # Avoid double update if already marked failed and broken
                    logger.debug(f"[AGENT] Updating step {current_task_index+1} status to '{new_status}' with result='{str(update_result)[:100]}...', error='{update_error}'")
                    self.task_manager.update_subtask_status(current_task_index, new_status, result=update_result, error=update_error)

                # --- Clear Highlights After Action (if applicable) ---
                if self._latest_dom_state and not self.browser_controller.headless:
                     try:
                          logger.debug("Clearing highlights from previous step...")
                          # Ensure page object exists
                          if self.browser_controller.page:
                              self.browser_controller.page.evaluate('document.getElementById("playwright-highlight-container")?.remove()')
                          else:
                               logger.warning("Cannot clear highlights, page object is None.")
                     except Exception as clear_err:
                          logger.warning(f"Could not clear highlights: {clear_err}")


            # --- Loop End ---
            if run_status["status"] != "PASS" and iteration_count >= max_iterations_for_test:
                 run_status["message"] = f"⚠️ Test Stopped: Maximum iterations ({max_iterations_for_test}) reached."
                 run_status["error_details"] = run_status.get("error_details", "Max iterations reached before completion.")
                 run_status["status"] = "FAIL" # Ensure fail status if max iterations hit
                 logger.warning(run_status["message"])


            # --- Final Summary and Cleanup ---
            print(f"\n--- Test Run Finished ---")
            # Capture final console logs regardless of pass/fail for reporting
            try:
                run_status["all_console_messages"] = self.browser_controller.get_console_messages()
            except AttributeError:
                logger.warning("BrowserController has no 'get_console_messages'. Skipping.")
                run_status["all_console_messages"] = []


            print(f"Feature Tested: {run_status['feature']}")
            print(f"Result: {run_status['status']}")
            print(f"Message: {run_status['message']}")

            if run_status['status'] == 'FAIL':
                print("-" * 20 + " Failure Details " + "-" * 20)
                print(f"Failed Step #: {run_status.get('failed_step_index', 'N/A') + 1 if run_status.get('failed_step_index') is not None else 'N/A'}")
                print(f"Failed Step Desc: {run_status.get('failed_step_description', 'N/A')}")
                print(f"Error Details: {run_status.get('error_details', 'N/A')}")

                # Capture and report relevant console messages on failure
                relevant_failure_messages = [
                    msg for msg in run_status["all_console_messages"]
                    if msg['type'] in ['error', 'warning'] # Focus on errors/warnings
                ]
                max_shown_console = 5
                run_status["console_messages_on_failure"] = relevant_failure_messages[-max_shown_console:] # Get last few

                if run_status["console_messages_on_failure"]:
                    print("\n--- Console Errors/Warnings (Last {}): ---".format(len(run_status["console_messages_on_failure"])))
                    for msg in run_status["console_messages_on_failure"]:
                        print(f"- [{msg['type'].upper()}] {msg['text']}")
                    if len(relevant_failure_messages) > max_shown_console:
                        print(f"... (See full log/report for all {len(relevant_failure_messages)} errors/warnings)")
                else:
                    print("\n--- No Console Errors/Warnings captured. ---")

                # Try to save a final screenshot on failure
                try:
                    # Ensure output dir exists
                    os.makedirs("output", exist_ok=True)
                    failure_screenshot_path = os.path.join("output", f"failure_screenshot_{time.strftime('%Y%m%d_%H%M%S')}.png")
                    # Assuming save_screenshot exists and returns bool or path
                    saved_path = self.browser_controller.save_screenshot(failure_screenshot_path)
                    if saved_path:
                        run_status["screenshot_on_failure"] = saved_path
                        print(f"Screenshot on failure saved to: {saved_path}")
                    else:
                        print("Attempted to save screenshot on failure, but method indicated failure.")
                except AttributeError:
                     logger.warning("BrowserController has no 'save_screenshot' method. Skipping.")
                except Exception as ss_err:
                    logger.error(f"Could not save failure screenshot: {ss_err}")
                    print(f"Error saving failure screenshot: {ss_err}")
                print("-" * 57)


            final_summary = self.task_manager.get_progress_summary()
            run_status["steps_summary"] = final_summary
            print("\n📋 Final Steps Summary:")
            print(final_summary)
            if run_status["output_file"]: # Evidence file
                print(f"Evidence File: {run_status['output_file']}")


        except ValueError as e: # Catch planning errors specifically
             logger.critical(f"Test planning failed: {e}", exc_info=True)
             run_status["status"] = "FAIL"
             run_status["message"] = f"❌ Test Planning Failed: {e}"
             run_status["error_details"] = str(e)
        except Exception as e:
            logger.critical(f"An critical unexpected error occurred during the test run: {e}", exc_info=True)
            run_status["status"] = "FAIL" # Ensure failure status
            run_status["message"] = f"❌ Critical Error during test run: {e}"
            run_status["error_details"] = f"Unexpected Exception: {type(e).__name__}: {e}"
            # Attempt to capture console logs even on critical failure
            try:
                if self.browser_controller and self.browser_controller.page:
                    run_status["all_console_messages"] = self.browser_controller.get_console_messages()
                    run_status["console_messages_on_failure"] = [
                        msg for msg in run_status["all_console_messages"] if msg['type'] in ['error', 'warning']
                    ][-5:] # Last 5 errors/warnings
            except Exception as log_err:
                logger.error(f"Could not capture console logs after critical error: {log_err}")
        finally:
            logger.info("--- Ending Test Run ---")
            cleanup_start = time.time()
            logger.debug("[AGENT] Closing browser controller...")
            if hasattr(self, 'browser_controller') and self.browser_controller:
                 self.browser_controller.close()
            cleanup_end = time.time()
            logger.info(f"Browser cleanup took {cleanup_end - cleanup_start:.2f}s")

            end_time = time.time()
            run_status["duration_seconds"] = round(end_time - start_time, 2)
            # Get final summary safely
            try:
                 run_status["steps_summary"] = self.task_manager.get_progress_summary()
            except Exception as summary_e:
                 logger.error(f"Failed to get final task summary: {summary_e}")
                 run_status["steps_summary"] = "Error retrieving final summary."

            logger.info(f"Test run finished in {run_status['duration_seconds']:.2f} seconds.")
            logger.info(f"Final Run Status: {run_status['status']} - {run_status['message']}")
            logger.info(f"Final Steps Breakdown:\n{run_status['steps_summary']}")
            if run_status['status'] == 'FAIL':
                logger.warning(f"Failure Details: Step #{run_status.get('failed_step_index', 'N/A') + 1 if run_status.get('failed_step_index') is not None else 'N/A'}, Desc: '{run_status.get('failed_step_description', 'N/A')}', Error: {run_status.get('error_details', 'N/A')}")


        return run_status # Return the detailed status dictionary