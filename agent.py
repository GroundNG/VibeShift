# agent.py

import json
import logging
import time
import re # Import regex
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
from typing import Dict, Any, Optional, List
import random
# Use relative imports within the package
from browser_controller import BrowserController
from llm_client import GeminiClient
from html_processor import HTMLProcessor
from vision_processor import VisionProcessor
from task_manager import TaskManager
import os

# Configure logger for this module specifically if needed, or rely on root config
logger = logging.getLogger(__name__)

# --- Debugging Settings ---
ARTIFICIAL_DELAY_SECS = 7.0 # Adjust delay duration as needed (e.g., 2-5 seconds)
# --- End Debugging Settings ---

class WebAgent:
    """
    Orchestrates the web browsing process using Playwright, Gemini (text and vision),
    HTML processing, and task management. Includes enhanced logging and delays for debugging.
    """

    def __init__(self,
                 gemini_client: GeminiClient,
                 headless: bool = True,
                 max_iterations: int = 25,
                 max_history_length: int = 8, # Keep history concise
                 max_retries_per_subtask: int = 2,
                 max_extracted_data_history: int = 5): # Limit stored extracted data for prompt):

        """
        Initializes the WebAgent.

        Args:
            gemini_client: An instance of the GeminiClient for LLM interactions.
            headless: Whether to run the browser in headless mode.
            max_iterations: Maximum number of iterations (action cycles) the agent can perform.
            max_history_length: Maximum number of history entries to keep for LLM context.
            max_retries_per_subtask: Maximum number of times to retry a failed subtask.
            max_extracted_data_history: Max number of recent extraction results to show LLM.

        """
        self.gemini_client = gemini_client
        self.browser_controller = BrowserController(headless=headless)
        self.html_processor = HTMLProcessor()
        self.vision_processor = VisionProcessor(gemini_client)
        self.task_manager = TaskManager(max_retries_per_subtask=max_retries_per_subtask)
        self.history: List[Dict[str, Any]] = []
        self.extracted_data_history: List[Dict[str, Any]] = []
        self.max_iterations = max_iterations
        self.max_history_length = max_history_length
        self.max_extracted_data_history = max_extracted_data_history
        self.output_file_path: Optional[str] = None
        self.feature_description: Optional[str] = None 
        
        logger.info(f"WebAgent (Test Mode) initialized (headless={headless}, max_iter={max_iterations}, max_hist={max_history_length}, max_retries={max_retries_per_subtask}, max_extract_hist={max_extracted_data_history}).")
        logger.debug(f"[DEBUG_AGENT] Artificial delay between steps set to: {ARTIFICIAL_DELAY_SECS}s")


    def _add_to_history(self, entry_type: str, data: Any):
        """Adds an entry to the agent's history, maintaining max length."""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_data_str = "..."
        try:
            if isinstance(data, dict):
                log_data = {k: (str(v)[:200] + '...' if isinstance(v, str) and len(v) > 200 else v)
                            for k, v in data.items()}
                log_data_str = json.dumps(log_data)
            elif isinstance(data, str):
                log_data = data[:297] + "..." if len(data) > 300 else data
                log_data_str = log_data
            else:
                log_data = data
                log_data_str = str(data)

            if len(log_data_str) > 300: log_data_str = log_data_str[:297]+"..."

        except Exception as e:
            logger.warning(f"Error sanitizing history data: {e}")
            log_data = f"Error processing data: {e}"
            log_data_str = log_data

        entry = {"timestamp": timestamp, "type": entry_type, "data": log_data}
        self.history.append(entry)
        if len(self.history) > self.max_history_length:
            self.history.pop(0)
        logger.debug(f"[HISTORY] Add: Type='{entry_type}', Data='{log_data_str}'")

    def _get_history_summary(self) -> str:
        """Provides a concise summary of the recent history for the LLM."""
        if not self.history:
            return "No history yet."
        summary = "Recent History (Oldest First):\n"
        for entry in self.history:
            entry_data_str = json.dumps(entry['data']) if isinstance(entry['data'], dict) else str(entry['data'])
            if len(entry_data_str) > 300:
                 entry_data_str = entry_data_str[:297] + "..."
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
            # --- FIX for unescaped quotes in specific values (selector, text) ---
            # Define a helper to escape quotes within a matched value
            def escape_quotes_in_value(match):
                prefix = match.group(1)  # Capture the part before the value ("key": ")
                value = match.group(2)   # Capture the value itself
                suffix = match.group(3)  # Capture the closing quote and terminator (", "}")
                # Escape only the double quotes *within* the value string
                # Using re.sub inside ensures we only affect the value part
                escaped_value = re.sub(r'(?<!\\)"', r'\\"', value) # Replace " with \" if not already escaped
                return f'{prefix}{escaped_value}{suffix}' # Reconstruct

            # Pattern Explanation:
            # \"(selector|text)\"\s*:\s*\"  -> Match "selector" or "text", colon, opening quote (Group 1)
            # (.*?)                         -> Non-greedily capture the value (Group 2)
            # \"(?=[\s,}\]])                -> Match the closing quote only if followed by whitespace, comma, } or ] (Group 3 includes the quote)
            # Using lookahead (?=...) for the terminator avoids consuming it, making replacement easier.
            # Let's slightly adjust the pattern capture groups for easier reconstruction:
            # Group 1: Key ("selector" or "text")
            # Group 2: Whitespace and colon (:\s*)
            # Group 3: Opening quote (")
            # Group 4: Value (.*?)
            # Group 5: Closing quote (")
            # Need a replacement function that uses group 4

            def escape_quotes_replacer(match):
                key_part = match.group(1) # "selector" or "text"
                colon_part = match.group(2) # :\s*
                open_quote = match.group(3) # "
                value = match.group(4)      # The actual value string
                close_quote = match.group(5) # "
                # Escape internal quotes
                escaped_value = re.sub(r'(?<!\\)"', r'\\"', value)
                return f'{key_part}{colon_part}{open_quote}{escaped_value}{close_quote}' # Rebuild

            pattern = re.compile(r'(\"(?:selector|text|reasoning)\")(\s*:\s*)(\")(.*?)(\")', re.DOTALL)
            json_str = pattern.sub(escape_quotes_replacer, json_str)
            logger.debug(f"[LLM PARSE] JSON string after attempting quote escaping: {json_str[:500]}...")
            # ------------------------------------------------------------------

            # --- Fix common escape sequences ---
            # Do this *after* fixing internal quotes
            json_str = json_str.replace('\\\\n', '\\n').replace('\\n', '\n') # Handle escaped newlines carefully
            json_str = json_str.replace('\\\\"', '\\"') # Handle already escaped quotes correctly
            # Add other common ones if needed: \\t -> \t etc.
            logger.debug(f"[LLM PARSE] JSON string after escape sequence cleaning: {json_str[:500]}...")

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
            logger.error(f"[LLM PARSE] Faulty JSON string snippet: {json_str[max(0, e.pos-50):e.pos+50]}") # Log around the error position
            logger.debug(f"[LLM PARSE] Full cleaned JSON string passed to loads:\n{json_str}") # Log full cleaned string on error
            self._add_to_history("LLM Parse Error", {"reason": f"JSONDecodeError: {e}", "error_pos": e.pos, "json_string_snippet": json_str[max(0, e.pos-50):e.pos+50]})
            return None

    def _plan_subtasks(self, feature_description: str):
        """Uses the LLM to break down the feature test into sequential, verifiable steps."""
        logger.info(f"Planning test steps for feature: '{feature_description}'")
        self.feature_description = feature_description # Store for context
        prompt = f"""
        You are an AI Test Engineer. Given the feature description for testing: "{feature_description}"

        Break this down into a sequence of specific, verifiable browser interaction steps that an automated web agent can perform using Playwright.
        Each step should represent a single logical action or verification.
        Focus on clarity, sequence, and using web element terminology. Be precise.

        Common Steps:
        1. Navigate to a specific URL.
        2. Locate elements (links, buttons, inputs, text areas).
        3. Interact (click, type specific text).
        4. **Verify:** Check for expected outcomes. This often involves:
            - Locating an element containing success/error text.
            - Using 'extract_text' on that element.
            - (Implicitly, the next step might involve checking this text, or you can specify the expected text in the description).
            - Example Verification Step: "Verify the text 'Login Successful' is present in the element with id 'status-message'".
            - Example Verification Step: "Extract the text from the element `span.welcome-user`". (The agent will need to know what text is expected later).
        5. Scroll if necessary to find elements.
        6. Extract data *if the test requires verifying specific data points*.

        **Output ONLY the test steps as a JSON list of strings.** Do not include explanations or markdown formatting outside the JSON list itself.

        Example Test Case: "Test login on example.com with user 'tester' and pass 'pwd123', then verify the welcome message."
        Example JSON Output:
        ```json
        [
          "Navigate to https://example.com/login",
          "Find the input field with name 'username' and type 'tester'",
          "Find the input field with name 'password' and type 'pwd123'",
          "Find the button with text 'Log In' and click it",
          "Verify the text 'Welcome, tester!' is present in the element with css selector 'h1.welcome-message'"
        ]
        ```

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
                 parsed_list = json.loads(json_str)
                 if isinstance(parsed_list, list) and all(isinstance(s, str) and s for s in parsed_list):
                     subtasks = parsed_list
                 else:
                      logger.warning(f"[TEST PLAN] Parsed JSON from markdown is not a list of non-empty strings: {parsed_list}")
             else:
                  # Attempt parsing as plain list if no markdown block
                  stripped_response = response.strip()
                  if stripped_response.startswith('[') and stripped_response.endswith(']'):
                       logger.debug("[TEST PLAN] Attempting to parse entire response as JSON list (no markdown found).")
                       try:
                           parsed_list = json.loads(stripped_response)
                           if isinstance(parsed_list, list) and all(isinstance(s, str) and s for s in parsed_list):
                               subtasks = parsed_list
                           else:
                               logger.warning(f"[TEST PLAN] Parsed JSON from full response is not a list of non-empty strings: {parsed_list}")
                       except json.JSONDecodeError:
                            logger.warning("[TEST PLAN] Could not parse entire response as JSON list.")

        except json.JSONDecodeError as e:
             logger.error(f"[TEST PLAN] Failed to decode JSON subtask list: {e}")
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
            # For testing, failing to plan is a critical failure. Don't add fallback.
            # raise ValueError("Failed to generate a valid test plan from the feature description.")



    def _get_extracted_data_summary(self) -> str:
        """Provides a concise summary of recently extracted data for the LLM (useful for verification steps)."""
        if not self.extracted_data_history:
            return "No data extracted yet."

        summary = "Recently Extracted Data (Most Recent First - useful for verification):\n"
        start_index = max(0, len(self.extracted_data_history) - self.max_extracted_data_history)
        for i, entry in enumerate(reversed(self.extracted_data_history[start_index:])):
             data_snippet = str(entry.get('data', ''))
             if len(data_snippet) > 150: data_snippet = data_snippet[:147] + "..."
             summary += f"- Step {entry.get('subtask_index', '?')+1} ('{entry.get('subtask_desc', 'N/A')[:50]}...'): Type={entry.get('type')}, Data={data_snippet}\n" # Added type
        return summary.strip()


    def _determine_next_action(self, current_task: Dict[str, Any], current_url: str, cleaned_html: str, screenshot_analysis: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Uses LLM to determine the specific browser action for the current test step."""
        logger.info(f"Determining next action for test step: '{current_task['description']}'")

        prompt = f"""
You are an expert AI web testing agent controller using Playwright. Your goal is to decide the *single next browser action* to perform to execute the current test step.

**Feature Under Test:** {self.feature_description}
**Current Test Step:** {current_task['description']}
**Current URL:** {current_url}
**Test Progress:** Attempt {current_task['attempts']} of {self.task_manager.max_retries_per_subtask + 1}. (Max retries: {self.task_manager.max_retries_per_subtask})

**CRITICAL INSTRUCTIONS FOR SELECTORS:**
- Use robust Playwright CSS selectors based on **NATIVE attributes** (id, name, class, data-testid, aria-label, placeholder, role, type, value, etc.) and visible text (`*:has-text("Visible Text")`).
- **DO NOT use `[data-ai-id="..."]` selectors.** These are for reference only.

**Available Actions (Output JSON Format):**
1.  `navigate`: Go to a URL.
    ```json
    {{"action": "navigate", "parameters": {{"url": "https://example.com"}}, "reasoning": "Navigate to the login page as per test step."}}
    ```
2.  `click`: Click an element.
    ```json
    {{"action": "click", "parameters": {{"selector": "button[type='submit']"}}, "reasoning": "Click the submit button."}}
    ```
3.  `type`: Type text into an input/textarea. **Use the exact text specified in the test step.**
    ```json
    {{"action": "type", "parameters": {{"selector": "input[name='username']", "text": "tester"}}, "reasoning": "Enter username."}}
    ```
4.  `scroll`: Scroll the page up or down (only if needed to find an element).
    ```json
    {{"action": "scroll", "parameters": {{"direction": "down"}}, "reasoning": "Scroll down to find the 'Next' button."}}
    ```
5.  `extract_text`: Get *visible text content* from an element. **Crucial for verification steps.** The result is stored.
    ```json
    {{"action": "extract_text", "parameters": {{"selector": "div.status-message"}}, "reasoning": "Extract text from the status message element to verify it later."}}
    ```
6.  `extract_attributes`: Get specific *attribute values* (like `href`, `src`, `value`) from an element. Useful for verifying attributes. Result (a dictionary) is stored.
    ```json
    {{"action": "extract_attributes", "parameters": {{"selector": "img.logo", "attributes": ["src"]}}, "reasoning": "Extract the logo image source URL for verification."}}
    ```
7.  `save_json`: **Rarely used in testing**, unless the test specifically requires saving collected data as evidence.
    ```json
    {{ "action": "save_json", "parameters": {{ "data": [...], "file_path": "test_evidence.json" }}, "reasoning": "Save extracted product details as test evidence." }}
    ```
8.  `subtask_complete`: Current test step finished successfully (e.g., after navigation, click, type). **Also use this if an `extract_text` or `extract_attributes` action successfully retrieved data needed for a *future* verification step.**
    ```json
    {{"action": "subtask_complete", "parameters": {{"result": "Successfully typed username."}}, "reasoning": "Username entered correctly."}}
    ```
    ```json
    {{"action": "subtask_complete", "parameters": {{"result": "Extracted welcome message text."}}, "reasoning": "Text extracted, verification will happen based on this in subsequent analysis if needed, or if the presence itself is the verification."}}
    ```
9.  `final_answer`: **Rarely used in testing**. Only if the test result is a simple text answer derived directly *without* file saving.
    ```json
    {{"action": "final_answer", "parameters": {{"answer": "Site health check passed."}}, "reasoning": "Basic site availability confirmed."}}
    ```
10. `subtask_failed`: Current test step **cannot be completed successfully** or **a verification failed**. Explain why clearly.
     ```json
     {{"action": "subtask_failed", "parameters": {{"reason": "Login button element ('button.login') not found."}}, "reasoning": "Cannot proceed with login."}}
     ```
     ```json
     {{"action": "subtask_failed", "parameters": {{"reason": "Verification failed: Expected text 'Welcome, tester!' but found 'Invalid credentials' in selector 'h1.welcome-message'."}}, "reasoning": "Login appears to have failed based on verification text."}}
     ```
     ```json
     {{"action": "subtask_failed", "parameters": {{"reason": "Element 'div.status-message' for verification was not found."}}, "reasoning": "Cannot verify outcome as expected element is missing."}}
     ```

**Input Context:**

**1. Recent Action History:**
{self._get_history_summary()}
"""

        if current_task['status'] == 'in_progress' and current_task['attempts'] > 1 and current_task.get('error'):
            error_context = str(current_task['error'])
            if len(error_context) > 300: error_context = error_context[:297] + "..."
            prompt += f"\n**Previous Attempt Error:**\nAttempt {current_task['attempts'] - 1} failed: {error_context}\nConsider this error when choosing the next action/selector. Maybe the selector was wrong, or the page didn't update as expected?\n"

        if screenshot_analysis:
            analysis_snippet = screenshot_analysis[:997] + "..." if len(screenshot_analysis) > 1000 else screenshot_analysis
            prompt += f"\n**2. Screenshot Analysis (Visual Context & Potential Selector Hints):**\n{analysis_snippet}\n*Use this visual info to find correct NATIVE attribute selectors in the HTML below.*\n"
        else:
            prompt += "\n**2. Screenshot Analysis:** Not available for this step.\n"


        prompt += f"\n**3. Recently Extracted Data (for Verification Steps):**\n{self._get_extracted_data_summary()}\n"
        prompt += "**If the current step is a verification, check if the extracted data matches the expected outcome described in the test step. If not, use `subtask_failed`.**\n"

        html_snippet_for_log = cleaned_html[:1000] + ('...' if len(cleaned_html)>1000 else '')
        logger.debug(f"[ACTION DECISION] Adding HTML Context to Prompt (Snippet)")
        prompt += f"\n**4. Current Page HTML Context (Cleaned, with ai-id references):**\n"
        prompt += cleaned_html

        prompt += """

**Your Decision:**
Based on the feature under test, the current test step, browser state, history, errors, and extracted data, determine the **single best next action** to execute the test step.
- If the step involves verification (e.g., "Verify text '...' is present"), use `extract_text` on the relevant element. If the extracted text (visible in history) doesn't match expectations, the *next* logical action might be `subtask_failed`. If the element itself cannot be found for extraction, use `subtask_failed`.
- Use `subtask_complete` after successful navigation, typing, clicking, or extraction *that doesn't immediately fail verification*.
- Use `subtask_failed` immediately if an action cannot be performed (e.g., element not found) or if a verification condition described in the step is clearly not met based on current state or extracted data.
- Ensure selectors use NATIVE attributes ONLY.
Output your decision strictly as a JSON object with brief reasoning related to the test step.

```json
"""
        # --- End Prompt Construction ---

        logger.debug(f"[ACTION DECISION] Pausing for {ARTIFICIAL_DELAY_SECS}s before calling LLM...")
        time.sleep(ARTIFICIAL_DELAY_SECS)

        response = self.gemini_client.generate_text(prompt)
        logger.debug(f"[ACTION DECISION] LLM Raw Response:\n{response[:500]}...")

        action_json = self._clean_llm_response_to_json(response)

        if action_json:
             reasoning = action_json.get("reasoning", "No reasoning provided.")
             decision_summary = {"action": action_json.get("action"), "parameters": action_json.get("parameters"), "reasoning": reasoning}
             logger.info(f"[LLM Decision] Action: {decision_summary['action']}, Params: {decision_summary['parameters']}, Reasoning: {reasoning[:150]}...")
             self._add_to_history("LLM Decision", decision_summary)
             return action_json
        else:
             self._add_to_history("LLM Decision Failed", {"raw_response_snippet": response[:500]+"..."})
             logger.error("[LLM Decision Failed] Failed to get a valid JSON action from LLM after parsing attempts.")
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
        """ Executes the browser action determined by the LLM for a test step. """
        action = action_details.get("action")
        params = action_details.get("parameters", {})
        result = {"success": False, "message": f"Action '{action}' not implemented or invalid.", "data": None}
        current_task_index = self._get_current_task_index(current_task_info["description"]) # Get index based on current task info


        if not action:
            result["message"] = "No action specified in LLM decision."
            logger.warning(f"[ACTION_EXEC] {result['message']}")
            self._add_to_history("Action Execution Error", {"reason": result["message"]})
            return result

        logger.info(f"[ACTION_EXEC] Attempting Action: {action} | Params: {params}")
        self._add_to_history("Executing Action", {"action": action, "parameters": params})

        # Add Delay Before Execution
        logger.debug(f"[DEBUG_AGENT] Pausing for {ARTIFICIAL_DELAY_SECS}s before executing action '{action}'...")
        time.sleep(ARTIFICIAL_DELAY_SECS)

        try:
            selector = params.get("selector")
            if selector and isinstance(selector, str) and 'data-ai-id' in selector:
                 raise ValueError("Invalid Selector: LLM provided a 'data-ai-id' selector. Selectors MUST use native attributes (id, class, name, text, etc.).")

            # --- Action Execution Logic (largely the same, adjusted logging/comments) ---
            if action == "navigate":
                url = params.get("url")
                if not url or not isinstance(url, str): raise ValueError("Missing or invalid 'url' parameter for navigate.")
                self.browser_controller.goto(url)
                result["success"] = True
                result["message"] = f"Successfully navigated to {url}."

            elif action == "click":
                if not selector or not isinstance(selector, str): raise ValueError("Missing or invalid 'selector' parameter for click.")
                self.browser_controller.click(selector)
                result["success"] = True
                result["message"] = f"Successfully clicked element: {selector}."

            elif action == "type":
                text = params.get("text") # Allow empty string ""
                if not selector or not isinstance(selector, str): raise ValueError("Missing or invalid 'selector' parameter for type.")
                if text is None or not isinstance(text, str): raise ValueError("Missing or invalid 'text' parameter for type.")
                self.browser_controller.type(selector, text)
                result["success"] = True
                result["message"] = f"Successfully typed text into element: {selector}."

            elif action == "scroll":
                direction = params.get("direction")
                if direction not in ["up", "down"]: raise ValueError("Invalid 'direction' parameter for scroll (must be 'up' or 'down').")
                self.browser_controller.scroll(direction)
                result["success"] = True
                result["message"] = f"Successfully scrolled {direction}."

            elif action == "extract_text":
                if not selector or not isinstance(selector, str): raise ValueError("Missing 'selector'.")
                extracted_data = self.browser_controller.extract_text(selector)
                if isinstance(extracted_data, str) and extracted_data.startswith("Error:"):
                     # Propagate the specific error message from browser_controller
                     raise PlaywrightError(f"Text extraction failed: {extracted_data}")
                result["success"] = True
                result["message"] = f"Extracted text from: {selector}."
                result["data"] = extracted_data
                # Store extracted data for potential verification
                if current_task_index != -1:
                     entry = {
                         "subtask_index": current_task_index,
                         "subtask_desc": current_task_info["description"],
                         "type": "text",
                         "data": extracted_data,
                         "selector": selector
                     }
                     self.extracted_data_history.append(entry)
                     logger.info(f"Stored extracted text from test step {current_task_index + 1} for potential verification.")
                     logger.debug(f"[EXTRACTED DATA] Added: {entry}")
                else:
                     logger.warning("[ACTION_EXEC] Could not determine test step index for storing extracted data.")


            elif action == "extract_attributes":
                attributes = params.get("attributes")
                if not selector or not isinstance(selector, str): raise ValueError("Missing 'selector'.")
                if not attributes or not isinstance(attributes, list) or not all(isinstance(a, str) for a in attributes):
                    raise ValueError("Missing or invalid 'attributes' list parameter.")
                extracted_data_dict = self.browser_controller.extract_attributes(selector, attributes)
                if extracted_data_dict.get("error"):
                    raise PlaywrightError(f"Attribute extraction failed: {extracted_data_dict['error']}")
                result["success"] = True
                result["message"] = f"Extracted attributes {attributes} from: {selector}."
                result["data"] = extracted_data_dict
                # Store extracted data for potential verification
                if current_task_index != -1:
                    entry = {
                        "subtask_index": current_task_index,
                        "subtask_desc": current_task_info["description"],
                        "type": "attributes",
                        "data": extracted_data_dict,
                        "selector": selector
                    }
                    self.extracted_data_history.append(entry)
                    logger.info(f"Stored extracted attributes from test step {current_task_index + 1} for potential verification.")
                    logger.debug(f"[EXTRACTED DATA] Added: {entry}")
                else:
                     logger.warning("[ACTION_EXEC] Could not determine test step index for storing extracted attributes.")

            elif action == "save_json": # Less common for testing, but possible for evidence
                data_to_save = params.get("data")
                file_path = params.get("file_path")
                default_filename = "test_evidence.json"
                if data_to_save is None: raise ValueError("Missing 'data' parameter for save_json.")
                if not file_path or not isinstance(file_path, str): file_path = default_filename
                if ".." in file_path: raise ValueError("Invalid file_path: contains '..'.")
                # Ensure output directory exists
                output_dir = "output"
                if not os.path.isabs(file_path) and os.path.dirname(file_path) != output_dir:
                     file_path = os.path.join(output_dir, file_path)
                save_result = self.browser_controller.save_json_data(data_to_save, file_path)
                result["success"] = save_result["success"]
                result["message"] = save_result["message"]
                if save_result["success"]:
                    result["data"] = {"file_path": save_result.get("file_path")}
                    self.output_file_path = save_result.get("file_path") # Store path if saved
                else:
                    result["message"] = f"Failed to save JSON evidence: {save_result.get('message', 'Unknown save error')}"

            elif action == "final_answer": # Very rare for testing
                answer = params.get("answer", "No specific answer provided.")
                result["success"] = True
                result["message"] = "Final answer determined by LLM (rare for tests)."
                result["data"] = answer
                logger.info(f"[ACTION_EXEC] LLM provided final answer: {str(answer)[:500]}...")

            elif action == "subtask_complete": # Step completed successfully
                 subtask_result = params.get("result", "Test step completed successfully.")
                 result["success"] = True
                 result["message"] = "Test step marked as complete by LLM."
                 result["data"] = subtask_result
                 logger.info(f"[ACTION_EXEC] LLM marked test step as complete. Result: {str(subtask_result)[:500]}...")

            elif action == "subtask_failed": # Step failed verification or execution
                 reason = params.get("reason", "LLM determined test step failed.")
                 result["success"] = False # Explicitly False for failure
                 result["message"] = f"Test step marked as failed by LLM: {reason}"
                 logger.warning(f"[ACTION_EXEC] {result['message']}")

            else:
                result["message"] = f"Unknown action requested by LLM: {action}"
                logger.error(f"[ACTION_EXEC] {result['message']}")

        except (PlaywrightError, PlaywrightTimeoutError) as e:
            error_msg = f"Playwright Action '{action}' Failed: {type(e).__name__}: {str(e)}"
            logger.error(f"[ACTION_EXEC] {error_msg}", exc_info=False) # Don't need full stack trace here usually
            result["message"] = error_msg
            result["success"] = False
        except ValueError as e: # Catch bad parameters or invalid selectors
            error_msg = f"Action '{action}' Input Error: {e}"
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
        if result.get('data') and action not in ["extract_text", "extract_attributes"]: # Avoid logging extracted data twice
            logger.debug(f"[ACTION_RESULT] Data snippet: {str(result.get('data'))[:100]}...")
        self._add_to_history("Action Result", {"success": result["success"], "message": result["message"], "data_snippet": str(result.get('data'))[:100]+"..." if result.get('data') else None})

        # Add Delay After Execution
        logger.debug(f"[DEBUG_AGENT] Pausing for {ARTIFICIAL_DELAY_SECS}s after executing action '{action}'...")
        time.sleep(ARTIFICIAL_DELAY_SECS)

        return result



    def run(self, feature_description: str):
        """
        Runs the automated test for the given feature description.
        Captures and reports console messages on failure.
        """
        logger.info(f"--- Starting Test Run --- Feature: {feature_description}")
        print(f"\n--- Starting Test Run for Feature ---\n{feature_description}\n" + "-"*35)
        start_time = time.time()
        run_status = {
            "status": "FAIL", # Default to FAIL
            "feature": feature_description,
            "message": "Test run initiated.",
            "failed_step_index": None,
            "failed_step_description": None,
            "error_details": None,
            "output_file": None,
            "screenshot_on_failure": None,
            "console_messages_on_failure": [], # <-- Add field for relevant messages on failure
            "all_console_messages": [], # <-- Add field for all messages (for report)
            "duration_seconds": 0.0,
            "steps_summary": "Not started.",
            "total_steps": 0,
            "steps_completed": 0,
        }
        self.history = []
        self.extracted_data_history = []
        self.output_file_path = None
        all_console_messages = [] # Local variable to accumulate all messages



        try:
            logger.debug("[DEBUG_AGENT] Starting browser controller...")
            self.browser_controller.start()
            # --- Clear previous console messages ---
            self.browser_controller.clear_console_messages()
            # --------------------------------------
            self.task_manager.set_main_task(feature_description)
            logger.debug("[DEBUG_AGENT] Planning test steps...")
            self._plan_subtasks(feature_description)

            if not self.task_manager.subtasks:
                 run_status["message"] = "❌ Test Planning Failed: Could not generate test steps."
                 run_status["error_details"] = "LLM failed to return a valid list of steps."
                 logger.error(run_status["message"])
                 # Capture any console messages even during planning failure if browser started
                 run_status["all_console_messages"] = self.browser_controller.get_console_messages()
                 run_status["console_messages_on_failure"] = [
                     msg for msg in run_status["all_console_messages"]
                     if msg['type'] in ['error', 'warning']
                 ]
                 # --- Cleanup and Return Failure ---
                 logger.info("--- Ending Test Run (Planning Failed) ---")
                 self.browser_controller.close()
                 end_time = time.time()
                 run_status["duration_seconds"] = round(end_time - start_time, 2)
                 return run_status



            run_status["total_steps"] = len(self.task_manager.subtasks)
            iteration_count = 0
            max_iterations_for_test = self.max_iterations

            while iteration_count < max_iterations_for_test:
                iteration_count += 1
                logger.info(f"\n===== Test Iteration {iteration_count}/{max_iterations_for_test} =====")
                print(f"\nIteration {iteration_count}/{max_iterations_for_test}...")

                logger.debug("[DEBUG_AGENT] Getting next test step...")
                current_task = self.task_manager.get_next_subtask()

                if not current_task:
                    # Check if all tasks are actually done successfully
                    if self.task_manager.is_complete():
                         all_done = all(t['status'] == 'done' for t in self.task_manager.subtasks)
                    if self.task_manager.is_complete():
                         if all_done:
                              run_status["status"] = "PASS"
                              run_status["message"] = "✅ Test Passed: All steps executed successfully."
                              run_status["steps_completed"] = len(self.task_manager.subtasks)
                         else:
                              logger.warning("[TEST RESULT] Test finished, but not all steps were 'done'. Check summary.")
                              # Find first non-done task to report failure reason more accurately
                              first_failed_task = None
                              first_failed_idx = -1
                              for idx, task in enumerate(self.task_manager.subtasks):
                                   if task['status'] != 'done':
                                       first_failed_task = task
                                       first_failed_idx = idx
                                       break
                              if first_failed_task:
                                   run_status["message"] = f"❌ Test Failed: Step {first_failed_idx + 1} did not complete successfully (Status: {first_failed_task['status']})."
                                   run_status["failed_step_index"] = first_failed_idx
                                   run_status["failed_step_description"] = first_failed_task['description']
                                   run_status["error_details"] = first_failed_task.get('error', 'Step ended with non-done status.')
                              else: # Should not happen if is_complete is true and not all done
                                   run_status["message"] = "❌ Test Failed: Finished processing, but state inconsistency detected (not all steps 'done')."
                    else:
                         run_status["message"] = "❌ Test Error: Step loop ended unexpectedly."
                         run_status["error_details"] = "TaskManager returned no next step, but is_complete() is false."
                         logger.error(run_status["message"])
                    break # Exit loop

                # Get index AFTER get_next_subtask marks it as in_progress
                current_task_index = self._get_current_task_index(current_task["description"])
                if current_task_index == -1:
                     run_status["message"] = f"❌ Critical Error: Could not determine index for current step: {current_task['description']}. Aborting test."
                     run_status["error_details"] = "Internal state error tracking current step index."
                     logger.critical(run_status["message"])
                     break # Critical failure

                run_status["steps_completed"] = current_task_index # Steps completed are up to the one *before* this

                logger.info(f"Current Test Step #{current_task_index + 1}/{run_status['total_steps']} (Attempt {current_task['attempts']}): {current_task['description']}")
                print(f"Executing Step {current_task_index + 1}: {current_task['description']} (Attempt {current_task['attempts']})")
                logger.debug(f"[DEBUG_AGENT] Step Details: Index={current_task_index}, Status={current_task['status']}, Attempts={current_task['attempts']}, Error='{current_task.get('error')}', LastFailedSelector='{current_task.get('last_failed_selector')}'")


                # --- State Gathering ---
                logger.info("Gathering current browser state...")
                current_url = "Error: Could not get URL"
                html_content = ""
                cleaned_html = "Error: Could not process HTML"
                screenshot_bytes = None
                screenshot_analysis = None

                try:
                    current_url = self.browser_controller.get_current_url()
                    if not current_url.startswith("Error"):
                        html_content = self.browser_controller.get_html()
                        if not html_content.startswith("Error"):
                            cleaned_html = self.html_processor.clean_html(html_content)
                        else:
                            cleaned_html = html_content
                            logger.warning(f"[STATE] Failed to get raw HTML: {html_content}")
                    else:
                        cleaned_html = f"Error getting URL: {current_url}"


                    # Take screenshot, analyze ONLY if it's a retry attempt on a failed step
                    screenshot_bytes = self.browser_controller.take_screenshot()
                    if screenshot_bytes and current_task['attempts'] > 1 and current_task.get('error'): # Analyze only on retry
                             logger.info("[STATE] Retry detected. Getting screenshot analysis...")
                             failed_selector = current_task.get('last_failed_selector')
                             error_context = current_task.get('error')
                             logger.debug(f"[STATE] Requesting vision analysis for step: '{current_task['description']}', Failed Selector: '{failed_selector}', Error: '{error_context}'")
                             screenshot_analysis = self.vision_processor.analyze_screenshot_for_action(
                                  screenshot_bytes, current_task['description'], failed_selector, error_context
                             )
                             analysis_snippet = screenshot_analysis[:300] + ('...' if len(screenshot_analysis)>300 else '')
                             logger.debug(f"[STATE] Received screenshot analysis snippet:\n{analysis_snippet}")
                             self._add_to_history("Screenshot Analysis", analysis_snippet)
                    elif screenshot_bytes:
                        logger.debug("[STATE] Took screenshot, but not analyzing (first attempt or no error).")
                    else:
                        logger.warning("[STATE] Failed to take screenshot.")

                except Exception as e:
                    logger.error(f"Failed to gather browser state: {e}", exc_info=True)
                    cleaned_html = f"Error gathering page state: {e}"
                    current_url = "Error gathering page state"
                    # Potentially mark step as failed here? Or let action determination handle it? Let LLM try.

                # --- Action Determination ---
                action_decision = self._determine_next_action(
                    current_task, current_url, cleaned_html, screenshot_analysis
                )

                if not action_decision:
                    logger.error("LLM failed to provide a valid action. Marking step as failed.")
                    error_msg = "LLM failed to determine a valid action."
                    # Directly update task status to failed for this attempt
                    self.task_manager.update_subtask_status(current_task_index, "failed", error=error_msg)
                    # Log failure details for final report
                    run_status["message"] = f"❌ Test Failed: LLM could not decide action for step {current_task_index + 1}."
                    run_status["failed_step_index"] = current_task_index
                    run_status["failed_step_description"] = current_task['description']
                    run_status["error_details"] = error_msg
                    logger.warning("[DEBUG_AGENT] Adding delay after LLM action determination failure...")
                    time.sleep(random.uniform(1.0, 2.0)) # Shorter delay?
                    # Check if retries are exhausted
                    if current_task['attempts'] >= self.task_manager.max_retries_per_subtask:
                         break # No more retries, exit loop
                    else:
                         continue # Allow retry on next iteration


                # --- Action Execution ---
                execution_result = self._execute_action(action_decision, current_task)
                action_type = action_decision.get("action")
                last_selector_used = action_decision.get("parameters", {}).get("selector")

                # --- Update Test Step Status ---
                update_result = execution_result.get("data", execution_result["message"])
                update_error = None if execution_result["success"] else execution_result["message"]
                new_status = "unknown"

                if execution_result["success"]:
                    # Handle actions that might end the test successfully (less common)
                    if action_type == "final_answer":
                        final_data = execution_result.get('data', "Test goal achieved.")
                        logger.info(f"Test step resulted in final answer: {str(final_data)[:500]}...")
                        new_status = "done"
                        # This doesn't necessarily mean the whole test passed yet.
                    elif action_type == "save_json":
                         file_path = execution_result.get("data", {}).get("file_path", "N/A")
                         logger.info(f"Test evidence saved to file: {file_path}")
                         run_status["output_file"] = file_path # Record evidence file
                         new_status = "done"
                         # Saving evidence doesn't mean the test passed yet.
                    elif action_type == "subtask_complete":
                         subtask_data = execution_result.get('data', "Step completed.")
                         logger.info(f"Test step marked complete by LLM. Result: {str(subtask_data)[:500]}...")
                         new_status = "done"
                    else: # Other successful browser actions (navigate, click, type, extract)
                         logger.info(f"Action '{action_type}' executed successfully for step {current_task_index + 1}.")
                         new_status = "done" # Mark step as done
                else: # Execution failed OR subtask_failed action
                    error_msg = execution_result["message"]
                    logger.warning(f"Action '{action_type}' failed for test step #{current_task_index + 1}. Error: {error_msg}")
                    new_status = "failed" # Mark the current attempt as failed
                    if last_selector_used:
                         # Record the selector that failed for this attempt
                         self.task_manager.subtasks[current_task_index]['last_failed_selector'] = last_selector_used
                         logger.debug(f"[DEBUG_AGENT] Recorded last failed selector for step {current_task_index+1}: {last_selector_used}")
                    # Store the error for the task manager and potentially the final report
                    update_error = error_msg

                    # --- Immediate Failure Handling ---
                    # If a step fails and has no more retries, stop the test run.
                    if current_task['attempts'] >= self.task_manager.max_retries_per_subtask:
                        logger.error(f"Test step {current_task_index + 1} failed permanently after {current_task['attempts']} attempts. Stopping test.")
                        run_status["message"] = f"❌ Test Failed: Step {current_task_index + 1} failed permanently."
                        run_status["failed_step_index"] = current_task_index
                        run_status["failed_step_description"] = current_task['description']
                        run_status["error_details"] = update_error
                        # Update status before breaking
                        self.task_manager.update_subtask_status(current_task_index, new_status, result=None, error=update_error)
                        break # Exit the loop immediately on permanent failure
                    else:
                         logger.warning(f"Test step {current_task_index + 1} failed, retrying (Attempt {current_task['attempts']+1}).")
                         # Add delay after recoverable failure before retry
                         logger.warning("[DEBUG_AGENT] Adding delay after action execution failure...")
                         time.sleep(random.uniform(1.5, 3.0))


                # Update task status via TaskManager AFTER checking for break condition
                logger.debug(f"[DEBUG_AGENT] Updating step {current_task_index+1} status to '{new_status}' with result='{str(update_result)[:100]}...', error='{update_error}'")
                # Need to pass index explicitly as current_task_index might lag if get_next advances
                self.task_manager.update_subtask_status(current_task_index, new_status, result=update_result, error=update_error)


            # --- Loop End ---
            if run_status["status"] != "PASS" and iteration_count >= max_iterations_for_test:
                 run_status["message"] = f"⚠️ Test Stopped: Maximum iterations ({max_iterations_for_test}) reached."
                 run_status["error_details"] = run_status.get("error_details", "Max iterations reached before completion.") # Keep existing error if already set
                 logger.warning(run_status["message"])
                 # Ensure status is FAIL if not already PASS
                 run_status["status"] = "FAIL"


            # --- Final Summary and Cleanup ---
            print(f"\n--- Test Run Finished ---")
            all_console_messages = self.browser_controller.get_console_messages()
            run_status["all_console_messages"] = all_console_messages
            print(f"Feature Tested: {run_status['feature']}")
            print(f"Result: {run_status['status']}")
            print(f"Message: {run_status['message']}")
            if run_status['status'] == 'FAIL':
                relevant_failure_messages = [
                    msg for msg in all_console_messages
                    if msg['type'] in ['error', 'warning']
                ]
                max_shown = 5
                run_status["console_messages_on_failure"] = relevant_failure_messages[-max_shown:]
                print(f"Feature Tested: {run_status['feature']}")
                print(f"Result: {run_status['status']}")
                print(f"Message: {run_status['message']}")
                print(f"Failed Step #: {run_status.get('failed_step_index', 'N/A') + 1 if run_status.get('failed_step_index') is not None else 'N/A'}")
                print(f"Failed Step Desc: {run_status.get('failed_step_description', 'N/A')}")
                print(f"Error Details: {run_status.get('error_details', 'N/A')}")
                # Try to save a final screenshot on failure
                if run_status["console_messages_on_failure"]:
                    print("\n--- Console Errors/Warnings (Last {}): ---".format(len(run_status["console_messages_on_failure"])))
                    for msg in run_status["console_messages_on_failure"]:
                        print(f"- [{msg['type'].upper()}] {msg['text']}")
                    if len(relevant_failure_messages) > max_shown:
                        print(f"... (See full log in saved JSON for all {len(relevant_failure_messages)} errors/warnings)")
                else:
                    print("\n--- No Console Errors/Warnings captured during failure. ---")
                try:
                    failure_screenshot_path = os.path.join("output", f"failure_screenshot_{time.strftime('%Y%m%d_%H%M%S')}.png")
                    saved = self.browser_controller.save_screenshot(failure_screenshot_path)
                    if saved:
                        run_status["screenshot_on_failure"] = failure_screenshot_path
                        print(f"Screenshot on failure saved to: {failure_screenshot_path}")
                    else:
                        print("Failed to save screenshot on failure.")
                except Exception as ss_err:
                    logger.error(f"Could not save failure screenshot: {ss_err}")
                    print(f"Error saving failure screenshot: {ss_err}")


            print("\n📋 Final Steps Summary:")
            final_summary = self.task_manager.get_progress_summary()
            run_status["steps_summary"] = final_summary # Store in status
            print(final_summary)
            if run_status["output_file"]:
                print(f"Evidence File: {run_status['output_file']}")


        except Exception as e:
            logger.critical(f"An critical unexpected error occurred during the test run: {e}", exc_info=True)
            run_status["status"] = "FAIL" # Ensure failure status
            run_status["message"] = f"❌ Critical Error during test run: {e}"
            run_status["error_details"] = f"Unexpected Exception: {type(e).__name__}: {e}"
            try:
                if self.browser_controller and self.browser_controller.page:
                    all_console_messages = self.browser_controller.get_console_messages()
                    run_status["all_console_messages"] = all_console_messages
                    run_status["console_messages_on_failure"] = [
                        msg for msg in all_console_messages if msg['type'] in ['error', 'warning']
                    ][-5:] # Last 5 errors/warnings
            except Exception as log_err:
                logger.error(f"Could not capture console logs after critical error: {log_err}")
        finally:
            logger.info("--- Ending Test Run ---")
            cleanup_start = time.time()
            logger.debug("[DEBUG_AGENT] Closing browser controller...")
            self.browser_controller.close()
            cleanup_end = time.time()
            logger.info(f"Browser cleanup took {cleanup_end - cleanup_start:.2f}s")

            end_time = time.time()
            run_status["duration_seconds"] = round(end_time - start_time, 2)
            # Get final summary again in case of errors during run
            try:
                 run_status["steps_summary"] = self.task_manager.get_progress_summary()
            except Exception as summary_e:
                 logger.error(f"Failed to get final task summary: {summary_e}")
                 run_status["steps_summary"] = "Error retrieving final summary."

            logger.info(f"Test run finished in {run_status['duration_seconds']:.2f} seconds.")
            logger.info(f"Final Run Status: {run_status['status']} - {run_status['message']}")
            # Log final summary at INFO level
            logger.info(f"Final Steps Breakdown:\n{run_status['steps_summary']}")
            if run_status['status'] == 'FAIL':
                logger.warning(f"Failure Details: Step #{run_status.get('failed_step_index', 'N/A')}, Desc: '{run_status.get('failed_step_description', 'N/A')}', Error: {run_status.get('error_details', 'N/A')}")
                failure_console_count = len(run_status.get("console_messages_on_failure", []))
                if failure_console_count > 0:
                    logger.warning(f"Captured {failure_console_count} console errors/warnings related to failure (see details above or in JSON report).")


        return run_status # Return the detailed status dictionary