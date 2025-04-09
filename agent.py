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
                 max_iterations: int = 15,
                 max_history_length: int = 6, # Keep history concise
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
        logger.info(f"WebAgent initialized (headless={headless}, max_iter={max_iterations}, max_hist={max_history_length}, max_retries={max_retries_per_subtask}, max_extract_hist={max_extracted_data_history}).")
        logger.debug(f"[DEBUG_AGENT] Artificial delay between steps set to: {ARTIFICIAL_DELAY_SECS}s")


    def _add_to_history(self, entry_type: str, data: Any):
        """Adds an entry to the agent's history, maintaining max length."""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        # Basic data sanitization/truncation for history
        log_data_str = "..." # Placeholder
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

            if len(log_data_str) > 300: log_data_str = log_data_str[:297]+"..." # Ensure string representation is truncated

        except Exception as e:
            logger.warning(f"Error sanitizing history data: {e}")
            log_data = f"Error processing data: {e}"
            log_data_str = log_data

        entry = {"timestamp": timestamp, "type": entry_type, "data": log_data}
        self.history.append(entry)
        if len(self.history) > self.max_history_length:
            self.history.pop(0) # Remove oldest entry
        logger.debug(f"[DEBUG_AGENT] History Add: Type='{entry_type}', Data='{log_data_str}'")


    def _get_history_summary(self) -> str:
        """Provides a concise summary of the recent history for the LLM."""
        if not self.history:
            return "No history yet."
        summary = "Recent History (Oldest First):\n"
        for entry in self.history:
            entry_data_str = json.dumps(entry['data']) if isinstance(entry['data'], dict) else str(entry['data'])
            # Already truncated in _add_to_history, but double check length
            if len(entry_data_str) > 300:
                 entry_data_str = entry_data_str[:297] + "..."
            summary += f"- [{entry['type']}] {entry_data_str}\n" # Removed timestamp for brevity in prompt
        # logger.debug(f"[DEBUG_AGENT] Generated History Summary:\n{summary.strip()}") # Can be noisy
        return summary.strip()

    def _clean_llm_response_to_json(self, llm_output: str) -> Optional[Dict[str, Any]]:
        """
        Attempts to extract and parse JSON from the LLM's potentially messy output,
        looking for markdown code blocks first.
        """
        logger.debug(f"[DEBUG_AGENT] Attempting to parse LLM response (length: {len(llm_output)}).")
        # logger.debug(f"[DEBUG_AGENT] Raw LLM action output snippet:\n{llm_output[:500]}...") # Log raw output snippet

        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", llm_output, re.DOTALL | re.IGNORECASE)
        if match:
            json_str = match.group(1).strip()
            logger.debug(f"[DEBUG_AGENT] Extracted JSON from markdown block: {json_str[:500]}...")
        else:
            start_index = llm_output.find('{')
            end_index = llm_output.rfind('}')
            if start_index != -1 and end_index != -1 and end_index > start_index:
                json_str = llm_output[start_index : end_index + 1].strip()
                logger.debug(f"[DEBUG_AGENT] Attempting to parse extracted JSON between first {{ and last }}: {json_str[:500]}...")
            else:
                 logger.warning("[DEBUG_AGENT] Could not find JSON structure (markdown block or curly braces) in LLM output.")
                 self._add_to_history("LLM Parse Error", {"reason": "No JSON structure found", "raw_output_snippet": llm_output[:200]})
                 return None
        try:
             json_str = json_str.replace('\\\\n', '\\n').replace('\\\\"', '\\"')
        except Exception as clean_e:
             logger.warning(f"[DEBUG_AGENT] Error during pre-parsing cleaning: {clean_e}")

        try:
            parsed_json = json.loads(json_str)
            if isinstance(parsed_json, dict) and "action" in parsed_json and "parameters" in parsed_json:
                logger.debug(f"[DEBUG_AGENT] Successfully parsed action JSON: {parsed_json}")
                return parsed_json
            else:
                 logger.warning(f"[DEBUG_AGENT] Parsed JSON missing required keys ('action', 'parameters') or is not a dict: {parsed_json}")
                 self._add_to_history("LLM Parse Error", {"reason": "Missing required keys", "parsed_json": parsed_json})
                 return None
        except json.JSONDecodeError as e:
            logger.error(f"[DEBUG_AGENT] Failed to decode JSON from LLM output: {e}\nContent snippet: {json_str[:500]}...")
            self._add_to_history("LLM Parse Error", {"reason": f"JSONDecodeError: {e}", "json_string_snippet": json_str[:200]})
            return None

    def _plan_subtasks(self, main_goal: str):
        """Uses the LLM to break down the main goal into sequential subtasks."""
        logger.info("Planning subtasks for the main goal...")
        prompt = f"""
        Given the main goal: "{main_goal}"

        Break this down into a sequence of specific, actionable subtasks that an automated web browser agent can perform using Playwright.
        Each subtask should represent a single logical step.
        Focus on clarity, order, and using web element terminology. Be precise.

        Common steps:
        1. Navigate.
        2. Locate elements (links, buttons, inputs).
        3. Interact (click, type).
        4. Extract information:
            - Use specific steps for different pieces of data if needed (e.g., "Extract the product title text", then "Extract the product link URL").
        5. Scroll if needed.
        6. If the goal requires collecting multiple pieces of data, the VERY LAST subtask in that collection sequence MUST be to 'Collate the extracted data [mention what data, e.g., titles and links] and save it to a JSON file'.

        Output the subtasks ONLY as a JSON list of strings. Do not include explanations or markdown formatting outside the JSON list itself.

        Example Format (Extracting Title and Link):
        ```json
        [
          "Navigate to https://example-shop.com/products",
          "Find the first product item container on the page",
          "Extract the text of the product title from the container",
          "Extract the 'href' attribute (the URL) of the product link from the container",
          "Find the second product item container",
          "Extract the text of the product title from the second container",
          "Extract the 'href' attribute of the product link from the second container",
          "Collate the extracted titles and links and save them to a JSON file named 'products.json'"
        ]
        ```

        Now, generate the JSON list for the goal: "{main_goal}"
        JSON Subtask List:
        ```json
        """
        logger.debug(f"[DEBUG_AGENT] Sending Subtask Planning Prompt:\n{prompt[:500]}...") # Log snippet
        response = self.gemini_client.generate_text(prompt)
        logger.debug(f"[DEBUG_AGENT] LLM subtask planning RAW response:\n{response[:500]}...") # Log snippet

        subtasks = None
        try:
             match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", response, re.DOTALL | re.IGNORECASE)
             if match:
                 json_str = match.group(1).strip()
                 logger.debug("[DEBUG_AGENT] Extracted JSON list from markdown block for subtasks.")
                 parsed_list = json.loads(json_str)
                 if isinstance(parsed_list, list) and all(isinstance(s, str) and s for s in parsed_list):
                     subtasks = parsed_list
                 else:
                      logger.warning(f"[DEBUG_AGENT] Parsed JSON from markdown is not a list of non-empty strings: {parsed_list}")
             else:
                  stripped_response = response.strip()
                  if stripped_response.startswith('[') and stripped_response.endswith(']'):
                       logger.debug("[DEBUG_AGENT] Attempting to parse entire subtask response as JSON list (no markdown found).")
                       try:
                            parsed_list = json.loads(stripped_response)
                            if isinstance(parsed_list, list) and all(isinstance(s, str) and s for s in parsed_list):
                                subtasks = parsed_list
                            else:
                                logger.warning(f"[DEBUG_AGENT] Parsed JSON from full response is not a list of non-empty strings: {parsed_list}")
                       except json.JSONDecodeError:
                            logger.warning("[DEBUG_AGENT] Could not parse entire response as JSON list.")

        except json.JSONDecodeError as e:
             logger.error(f"[DEBUG_AGENT] Failed to decode JSON subtask list: {e}")
        except Exception as e:
            logger.error(f"[DEBUG_AGENT] An unexpected error occurred during subtask parsing: {e}", exc_info=True)

        if subtasks and len(subtasks) > 0:
            self.task_manager.add_subtasks(subtasks)
            self._add_to_history("Plan Created", {"main_goal": main_goal, "subtasks": subtasks})
            logger.info(f"Successfully planned {len(subtasks)} subtasks.")
            logger.debug(f"[DEBUG_AGENT] Planned Subtasks: {subtasks}")
        else:
            logger.error("[DEBUG_AGENT] Failed to generate or parse valid subtasks from LLM response.")
            self._add_to_history("Plan Failed", {"main_goal": main_goal, "raw_response": response[:500]+"..."})
            fallback_task = f"Attempt to achieve main goal directly: {main_goal}"
            self.task_manager.add_subtasks([fallback_task])
            logger.warning(f"[DEBUG_AGENT] Using fallback task: {fallback_task}")


    def _get_extracted_data_summary(self) -> str:
        """Provides a concise summary of recently extracted data for the LLM."""
        if not self.extracted_data_history:
            return "No data extracted yet."

        summary = "Recently Extracted Data (Most Recent First):\n"
        start_index = max(0, len(self.extracted_data_history) - self.max_extracted_data_history)
        relevant_history = self.extracted_data_history[start_index:]
        for entry in reversed(relevant_history):
             data_snippet = str(entry.get('data', ''))
             if len(data_snippet) > 150: data_snippet = data_snippet[:147] + "..."
             # Include the type (text/attributes) for clarity
             entry_type = entry.get('type', 'unknown')
             subtask_idx = entry.get('subtask_index', '?')
             subtask_desc_snippet = entry.get('subtask_desc', 'N/A')[:50] + "..."
             summary += f"- Task {subtask_idx + 1} ({entry_type}): '{subtask_desc_snippet}' -> {data_snippet}\n"
        # logger.debug(f"[DEBUG_AGENT] Generated Extracted Data Summary:\n{summary.strip()}") # Can be noisy
        return summary.strip()


    def _determine_next_action(self, current_task: Dict[str, Any], current_url: str, cleaned_html: str, screenshot_analysis: Optional[str] = None, vision_requested_previously: bool = False) -> Optional[Dict[str, Any]]:
        """Uses LLM to determine the specific browser action for the current subtask."""
        logger.info(f"Determining next action for subtask: '{current_task['description']}'")

        # --- Construct the Prompt ---
        prompt = f"""
You are an expert AI web agent controller using Playwright. Your goal is to decide the *single next browser action* to perform to make progress on the current subtask.

**Overall Goal:** {self.task_manager.main_task}
**Current Subtask:** {current_task['description']}
**Current URL:** {current_url}
**Task Progress:** Attempt {current_task['attempts']} of {self.task_manager.max_retries_per_subtask}.

**CRITICAL INSTRUCTIONS FOR SELECTORS:**
- Use robust Playwright CSS selectors based on **NATIVE attributes** (id, name, class, data-testid, aria-label, placeholder, role, type, value, etc.) and visible text (`*:has-text("Visible Text")`).
- **DO NOT use `[data-ai-id="..."]` selectors.**

**Available Actions (Output JSON Format):**
1.  `navigate`: Go to a URL.
    ```json
    {{"action": "navigate", "parameters": {{"url": "https://example.com"}}, "reasoning": "..."}}
    ```
2.  `click`: Click an element.
    ```json
    {{"action": "click", "parameters": {{"selector": "NATIVE_CSS_SELECTOR"}}, "reasoning": "..."}}
    ```
3.  `type`: Type text into an input/textarea.
    ```json
    {{"action": "type", "parameters": {{"selector": "NATIVE_CSS_SELECTOR", "text": "My text"}}, "reasoning": "..."}}
    ```
4.  `scroll`: Scroll the page up or down.
    ```json
    {{"action": "scroll", "parameters": {{"direction": "down" | "up"}}, "reasoning": "..."}}
    ```
5.  `extract_text`: Get *only the visible text content* from an element. Result is stored internally.
    ```json
    {{"action": "extract_text", "parameters": {{"selector": "NATIVE_CSS_SELECTOR"}}, "reasoning": "Extract the textual data."}}
    ```
6.  `extract_attributes`: Get specific *attribute values* (like `href`, `src`, `value`) from an element. Result (a dictionary) is stored internally.
    ```json
    {{"action": "extract_attributes", "parameters": {{"selector": "NATIVE_CSS_SELECTOR", "attributes": ["href", "title"]}}, "reasoning": "Extract the link URL and title attribute."}}
    ```
7.  `save_json`: **Use ONLY when ALL required data has been extracted. But DO USE IT AFTER ALL DATA HAS BEEN EXTRACTED. DO NOT FORGET**
    - **Crucially:** Examine the 'Recently Extracted Data' section below.
    - Combine related pieces of data into a meaningful structure.
    - Provide this structured data in the `data` parameter.
    - Specify a filename.
    ```json
    {{
      "action": "save_json",
      "parameters": {{
        "data": [...],
        "file_path": "extracted_items.json"
      }},
      "reasoning": "..."
    }}
    ```
8.  `subtask_complete`: Current subtask finished successfully (and no data needs saving *from this specific step*).
    ```json
    {{"action": "subtask_complete", "parameters": {{"result": "Successfully navigated."}}, "reasoning": "..."}}
    ```
9.  `final_answer`: Overall goal achieved *without* needing to save data to a file. Provide the textual answer.
    ```json
    {{"action": "final_answer", "parameters": {{"answer": "Website is online."}}, "reasoning": "..."}}
    ```
10. `request_vision_analysis`: **Use this if you are unsure about the correct element or action based only on the HTML and previous history.** This action will trigger a vision analysis of the current page state, and you will be prompted again with the visual context added. You can use this *even if the task hasn't failed yet*.
     ```json
     {{"action": "request_vision_analysis", "parameters": {{}}, "reasoning": "Uncertain about the correct button based on HTML, need visual confirmation."}}
     ```
11. `subtask_failed`: Current subtask cannot be completed. Explain why.
     ```json
     {{"action": "subtask_failed", "parameters": {{"reason": "Element not found after retries and vision analysis."}}, "reasoning": "..."}}
     ```

**Input Context:**

**1. Recent Action History:**
{self._get_history_summary()}
"""

        if current_task['status'] == 'in_progress' and current_task['attempts'] > 1 and current_task.get('error'):
            error_context = str(current_task['error'])
            if len(error_context) > 300: error_context = error_context[:297] + "..."
            prompt += f"\n**Previous Attempt Error:**\nAttempt {current_task['attempts'] - 1} failed: {error_context}\nConsider this error when choosing the next action/selector. Maybe try a different selector or scroll?\n"

        # Differentiate vision context based on whether it was automatic (retry) or requested
        if screenshot_analysis:
            analysis_snippet = screenshot_analysis[:997] + "..." if len(screenshot_analysis) > 1000 else screenshot_analysis
            if vision_requested_previously:
                prompt += f"\n**2. Screenshot Analysis (Requested by You):**\n{analysis_snippet}\n*Use this visual information along with the HTML context below to decide the next BROWSER action.*\n"
            else: # Automatic analysis on retry
                 prompt += f"\n**2. Screenshot Analysis (Visual Context on Retry):**\n{analysis_snippet}\n*Remember to generate selectors based on NATIVE attributes you see in the HTML context below, potentially informed by this visual analysis.*\n"
        else:
            prompt += "\n**2. Screenshot Analysis:** Not available for this step (unless requested).\n"


        prompt += f"\n**3. Recently Extracted Data (CRITICAL for 'save_json'):**\n{self._get_extracted_data_summary()}\n"
        prompt += "**If using 'save_json', ensure the `data` parameter below combines all relevant info from the history above into the final desired structure.**\n"

        # Log HTML context snippet before adding
        html_snippet_for_log = cleaned_html[:1000] + ('...' if len(cleaned_html)>1000 else '')
        logger.debug(f"[DEBUG_AGENT] Adding HTML Context to Prompt (Snippet)")
        prompt += f"\n**4. Current Page HTML Context (Cleaned):**\n" # Renumbered
        prompt += cleaned_html

        prompt += f"""

**Your Decision:**
Based on the goal, subtask, context, history, errors, and extracted data, determine the **single best next action**.
- If you are uncertain based on the current information (HTML, history), use `request_vision_analysis` first.
- After receiving vision analysis (if requested or on retry), choose a specific browser action (`click`, `type`, etc.) or conclude the task (`subtask_complete`, `save_json`, `final_answer`, `subtask_failed`).
- Ensure selectors use NATIVE attributes ONLY.
Output your decision strictly as a JSON object. Provide brief reasoning.

```json
"""
        # --- End Prompt Construction ---

        # --- Log the full prompt ---
        # logger.debug(f"[LLM_PROMPT] Sending following prompt to Gemini for action determination:\n{'-'*20} START PROMPT {'-'*20}\n{prompt}\n{'-'*21} END PROMPT {'-'*21}")

        # --- Add Delay Before LLM Call ---
        logger.debug(f"[DEBUG_AGENT] Pausing for {ARTIFICIAL_DELAY_SECS}s before calling LLM...")
        time.sleep(ARTIFICIAL_DELAY_SECS)
        # ------------------------------

        response = self.gemini_client.generate_text(prompt)

        # --- Log the raw response ---
        logger.debug(f"[LLM_RESPONSE_RAW] Received raw response from Gemini:\n{'-'*20} START RAW RESPONSE {'-'*20}\n{response}\n{'-'*21} END RAW RESPONSE {'-'*21}")
        # ----------------------------

        action_json = self._clean_llm_response_to_json(response) # Use the robust parser

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
        """Finds the index of the task currently being processed by its description."""
        for i, task in enumerate(self.task_manager.subtasks):
            if task["description"] == current_task_desc:
                 if task["status"] in ["in_progress", "failed"]:
                      logger.debug(f"[DEBUG_AGENT] Found active task index {i} for description: '{current_task_desc[:50]}...'")
                      return i
        # Fallback
        if 0 <= self.task_manager.current_subtask_index < len(self.task_manager.subtasks):
             task = self.task_manager.subtasks[self.task_manager.current_subtask_index]
             if task["description"] == current_task_desc:
                   logger.debug(f"[DEBUG_AGENT] Found task index {self.task_manager.current_subtask_index} via task_manager index for description: '{current_task_desc[:50]}...'")
                   return self.task_manager.current_subtask_index

        logger.warning(f"[DEBUG_AGENT] Could not reliably determine index for task: '{current_task_desc}'. Returning -1.")
        return -1


    def _execute_action(self, action_details: Dict[str, Any], current_task_info: Dict[str, Any]) -> Dict[str, Any]:
        """ Executes the browser action determined by the LLM, or handles vision requests. """
        action = action_details.get("action")
        params = action_details.get("parameters", {})
        reasoning = action_details.get("reasoning") # Capture reasoning for vision request
        result = {
            "success": False,
            "message": f"Action '{action}' not implemented or invalid.",
            "data": None,
            "needs_vision_retry": False, # Flag for vision request
            "reasoning": reasoning # Pass reasoning back if needed
        }
        current_task_index = self._get_current_task_index(current_task_info["description"])



        if not action:
            result["message"] = "No action specified in LLM decision."
            logger.warning(f"[ACTION_EXEC] {result['message']}")
            self._add_to_history("Action Execution Error", {"reason": result["message"]})
            return result

        logger.info(f"[ACTION_EXEC] Attempting Action: {action} | Params: {params}")
        # Only add browser actions to history here, vision request handled in run loop
        if action != "request_vision_analysis":
            self._add_to_history("Executing Action", {"action": action, "parameters": params})

        # --- Add Delay Before Execution (for browser actions) ---
        if action != "request_vision_analysis":
             logger.debug(f"[DEBUG_AGENT] Pausing for {ARTIFICIAL_DELAY_SECS}s before executing action '{action}'...")
             time.sleep(ARTIFICIAL_DELAY_SECS)
        # ---------------------------------

        try:
            selector = params.get("selector")
            if selector and isinstance(selector, str) and 'data-ai-id' in selector:
                 raise ValueError("Invalid Selector: LLM provided a 'data-ai-id' selector. Selectors MUST use native attributes (id, class, name, text, etc.).")

            # Handle the new vision request action
            if action == "request_vision_analysis":
                result["success"] = True # The request itself is successful
                result["needs_vision_retry"] = True
                result["message"] = "LLM requested vision analysis."
                logger.info(f"[ACTION_EXEC] LLM requested vision analysis. Reasoning: {reasoning}")
                # No browser interaction here, just return the result dict
            if action == "navigate":
                url = params.get("url")
                if not url or not isinstance(url, str): raise ValueError("Missing or invalid 'url' parameter for navigate.")
                self.browser_controller.goto(url)
                result["success"] = True
                result["message"] = f"Successfully navigated to {url}."
                # time.sleep(random.uniform(1.5, 2.5)) # Using fixed delay after action instead

            elif action == "click":
                if not selector or not isinstance(selector, str): raise ValueError("Missing or invalid 'selector' parameter for click.")
                self.browser_controller.click(selector)
                result["success"] = True
                result["message"] = f"Successfully clicked element: {selector}."
                # time.sleep(random.uniform(1.0, 2.0))

            elif action == "type":
                text = params.get("text") # Allow empty string ""
                if not selector or not isinstance(selector, str): raise ValueError("Missing or invalid 'selector' parameter for type.")
                if text is None or not isinstance(text, str): raise ValueError("Missing or invalid 'text' parameter for type.")
                self.browser_controller.type(selector, text)
                result["success"] = True
                result["message"] = f"Successfully typed text into element: {selector}."
                # time.sleep(random.uniform(0.5, 1.0))

            elif action == "scroll":
                direction = params.get("direction")
                if direction not in ["up", "down"]: raise ValueError("Invalid 'direction' parameter for scroll (must be 'up' or 'down').")
                self.browser_controller.scroll(direction)
                result["success"] = True
                result["message"] = f"Successfully scrolled {direction}."
                # time.sleep(random.uniform(0.5, 1.0))

            elif action == "extract_text":
                if not selector or not isinstance(selector, str): raise ValueError("Missing 'selector'.")
                extracted_data = self.browser_controller.extract_text(selector)
                if isinstance(extracted_data, str) and extracted_data.startswith("Error:"):
                     raise PlaywrightError(f"Extraction failed internally: {extracted_data}")
                result["success"] = True
                result["message"] = f"Extracted text from: {selector}."
                result["data"] = extracted_data
                if current_task_index != -1:
                     entry = {
                         "subtask_index": current_task_index,
                         "subtask_desc": current_task_info["description"],
                         "type": "text",
                         "data": extracted_data,
                         "selector": selector
                     }
                     self.extracted_data_history.append(entry)
                     logger.info(f"Stored extracted text from subtask {current_task_index + 1}.")
                     logger.debug(f"[DEBUG_AGENT] Added to extracted_data_history: {entry}")
                else:
                     logger.warning("[DEBUG_AGENT] Could not determine subtask index for storing extracted data.")

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
                if current_task_index != -1:
                    entry = {
                        "subtask_index": current_task_index,
                        "subtask_desc": current_task_info["description"],
                        "type": "attributes",
                        "data": extracted_data_dict,
                        "selector": selector
                    }
                    self.extracted_data_history.append(entry)
                    logger.info(f"Stored extracted attributes from subtask {current_task_index + 1}.")
                    logger.debug(f"[DEBUG_AGENT] Added to extracted_data_history: {entry}")
                else:
                     logger.warning("[DEBUG_AGENT] Could not determine subtask index for storing extracted attributes.")

            elif action == "save_json":
                data_to_save = params.get("data")
                file_path = params.get("file_path")
                default_filename = "agent_output.json"
                if data_to_save is None: raise ValueError("Missing 'data' parameter for save_json.")
                if not file_path or not isinstance(file_path, str): file_path = default_filename
                if ".." in file_path: raise ValueError("Invalid file_path: contains '..'.")
                if not os.path.dirname(file_path) and not os.path.isabs(file_path):
                    file_path = os.path.join("output", file_path)
                save_result = self.browser_controller.save_json_data(data_to_save, file_path)
                result["success"] = save_result["success"]
                result["message"] = save_result["message"]
                if save_result["success"]:
                    result["data"] = {"file_path": save_result.get("file_path")}
                    self.output_file_path = save_result.get("file_path")
                else:
                    result["message"] = f"Failed to save JSON: {save_result.get('message', 'Unknown save error')}"

            elif action == "final_answer":
                answer = params.get("answer", "No specific answer provided.")
                result["success"] = True
                result["message"] = "Final answer determined by LLM."
                result["data"] = answer
                logger.info(f"[ACTION_EXEC] LLM provided final answer: {str(answer)[:500]}...")

            elif action == "subtask_complete":
                 subtask_result = params.get("result", "Subtask completed successfully.")
                 result["success"] = True
                 result["message"] = "Subtask marked as complete by LLM."
                 result["data"] = subtask_result
                 logger.info(f"[ACTION_EXEC] LLM marked subtask as complete. Result: {str(subtask_result)[:500]}...")

            elif action == "subtask_failed":
                 reason = params.get("reason", "LLM determined subtask cannot be completed.")
                 result["success"] = False
                 result["message"] = f"Subtask marked as failed by LLM: {reason}"
                 logger.warning(f"[ACTION_EXEC] {result['message']}")

            else:
                result["message"] = f"Unknown action requested by LLM: {action}"
                logger.error(f"[ACTION_EXEC] {result['message']}")

        except (PlaywrightError, PlaywrightTimeoutError) as e:
            error_msg = f"Playwright Action '{action}' Failed: {type(e).__name__}: {str(e)}"
            logger.error(f"[ACTION_EXEC] {error_msg}", exc_info=False)
            result["message"] = error_msg
            result["success"] = False
        except ValueError as e:
            error_msg = f"Action '{action}' Input Error: {e}"
            logger.error(f"[ACTION_EXEC] {error_msg}")
            result["message"] = error_msg
            result["success"] = False
        except Exception as e:
            error_msg = f"Unexpected Error during action '{action}': {type(e).__name__}: {e}"
            logger.critical(f"[ACTION_EXEC] {error_msg}", exc_info=True)
            result["message"] = error_msg
            result["success"] = False

        # --- Log Action Result ---
        if action != "request_vision_analysis":
            log_level = logging.INFO if result["success"] else logging.WARNING
            logger.log(log_level, f"[ACTION_RESULT] Action '{action}' | Success: {result['success']} | Message: {result['message']}")
            if result.get('data'):
                logger.debug(f"[ACTION_RESULT] Data snippet: {str(result.get('data'))[:100]}...")
            self._add_to_history("Action Result", {"success": result["success"], "message": result["message"], "data_snippet": str(result.get('data'))[:100]+"..." if result.get('data') else None})
            
        # --- Add Delay After Execution ---
        logger.debug(f"[DEBUG_AGENT] Pausing for {ARTIFICIAL_DELAY_SECS}s after executing action '{action}'...")
        time.sleep(ARTIFICIAL_DELAY_SECS)
        # ---------------------------------

        return result



    def run(self, user_goal: str):
        """
        Starts the agent to achieve the user's goal through planning and execution cycles.
        Handles LLM requests for vision analysis within a task attempt.
        """
        logger.info(f"--- Starting Agent Run --- Goal: {user_goal}")
        start_time = time.time()
        run_status = {
            "success": False,
            "message": "Agent stopped.",
            "output_file": None,
            "final_answer": None,
            "duration_seconds": 0.0,
            "task_summary": "Not started.",
        }
        self.extracted_data_history = []
        self.output_file_path = None

        try:
            logger.debug("[DEBUG_AGENT] Starting browser controller...")
            self.browser_controller.start()
            self.task_manager.set_main_task(user_goal)
            logger.debug("[DEBUG_AGENT] Planning subtasks...")
            self._plan_subtasks(user_goal)

            if not self.task_manager.subtasks:
                run_status["message"] = "❌ Agent Error: Failed to plan any subtasks for the goal."
                logger.error(run_status["message"])
                raise Exception(run_status["message"])


            iteration_count = 0
            # Outer loop controls overall iterations/task progression
            while iteration_count < self.max_iterations:
                iteration_count += 1
                logger.info(f"\n===== Iteration {iteration_count}/{self.max_iterations} =====")
                print(f"\nIteration {iteration_count}/{self.max_iterations}...")

                logger.debug("[DEBUG_AGENT] Getting next subtask...")
                # Task manager handles retries internally by returning failed tasks if attempts remain
                current_task = self.task_manager.get_next_subtask()

                if not current_task:
                    # Handle completion or unexpected end
                    if self.task_manager.is_complete():
                        logger.info("[DEBUG_AGENT] All tasks processed according to TaskManager.")
                        # Check if goal was actually met via final_answer/save_json
                        if not run_status["success"]:
                            run_status["message"] = "🏁 Agent finished tasks, but goal completion wasn't explicitly confirmed by a final action (final_answer/save_json)."
                            logger.warning(run_status["message"])
                    else:
                        run_status["message"] = "❌ Agent Error: Task loop ended unexpectedly (get_next_subtask returned None but not complete)."
                        logger.error(run_status["message"])
                    break # Exit OUTER loop

                current_task_index = self._get_current_task_index(current_task["description"])
                if current_task_index == -1:
                    run_status["message"] = f"❌ Critical Error: Could not determine index for task: {current_task['description']}. Aborting."
                    logger.critical(run_status["message"]) # Critical error
                    break # Exit OUTER loop

                logger.info(f"Current Task #{current_task_index + 1} (Attempt {current_task['attempts']}): {current_task['description']}")
                print(f"Working on: {current_task['description']} (Attempt {current_task['attempts']})")
                logger.debug(f"[DEBUG_AGENT] Task Details: Index={current_task_index}, Status={current_task['status']}, Attempts={current_task['attempts']}, Error='{current_task.get('error')}', LastFailedSelector='{current_task.get('last_failed_selector')}'")


                # --- State variables FOR THIS ATTEMPT of the current task ---
                screenshot_analysis = None         # Holds analysis result (from retry or request)
                vision_requested_this_attempt = False # Flag if LLM asked for vision in this attempt
                vision_request_count = 0           # Counter for LLM vision requests in this attempt

                # --- Inner loop for Action-Decision cycle WITHIN ONE attempt ---
                # Allows re-prompting after a vision request without consuming a task attempt.
                while True:
                    logger.debug(f"[Inner Loop] Start action cycle for task {current_task_index+1} (Attempt {current_task['attempts']}, Vision Requested: {vision_requested_this_attempt}, Vision Req Count: {vision_request_count})")

                    # --- State Gathering (Refresh each inner loop pass) ---
                    logger.info("Gathering current browser state...")
                    current_url = "Error: Could not get URL"
                    html_content = ""
                    cleaned_html = "Error: Could not process HTML"
                    screenshot_bytes = None

                    try:
                        current_url = self.browser_controller.get_current_url()
                        logger.debug(f"[DEBUG_AGENT] Current URL: {current_url}")
                        if not current_url.startswith("Error"):
                            html_content = self.browser_controller.get_html()
                            if not html_content.startswith("Error"):
                                logger.debug(f"[DEBUG_AGENT] Raw HTML length: {len(html_content)}. Cleaning...")
                                cleaned_html = self.html_processor.clean_html(html_content)
                            else:
                                cleaned_html = html_content # Pass error message
                                logger.warning(f"[DEBUG_AGENT] Failed to get raw HTML: {html_content}")

                        screenshot_bytes = self.browser_controller.take_screenshot()
                        if not screenshot_bytes:
                            logger.warning("[DEBUG_AGENT] Failed to take screenshot.")

                    except Exception as e:
                        logger.error(f"Failed to gather browser state: {e}", exc_info=True)
                        cleaned_html = f"Error gathering page state: {e}"
                        current_url = "Error gathering page state"

                    # --- Automatic Vision Analysis (Only on Retry and ONLY IF not already analyzed in this attempt) ---
                    # Triggered if it's a retry (attempts > 1) AND vision wasn't explicitly requested yet in this attempt AND analysis hasn't been loaded yet
                    if not vision_requested_this_attempt and current_task['attempts'] > 1 and screenshot_bytes and not screenshot_analysis:
                        logger.info("[DEBUG_AGENT] Retry attempt detected. Getting automatic screenshot analysis...")
                        failed_selector = current_task.get('last_failed_selector')
                        error_context = current_task.get('error')
                        screenshot_analysis = self.vision_processor.analyze_screenshot_for_action(
                            screenshot_bytes, current_task['description'], failed_selector, error_context
                        )
                        analysis_snippet = screenshot_analysis[:200] + ('...' if len(screenshot_analysis)>200 else '')
                        self._add_to_history("Screenshot Analysis (Retry)", analysis_snippet)
                        logger.debug(f"[DEBUG_AGENT] Received automatic screenshot analysis snippet:\n{analysis_snippet}")


                    # --- Action Determination ---
                    # Pass current state, task info, and any available screenshot analysis
                    action_decision = self._determine_next_action(
                        current_task, current_url, cleaned_html, screenshot_analysis, vision_requested_this_attempt
                    )

                    # Handle case where LLM fails to decide
                    if not action_decision:
                        logger.error("LLM failed to provide a valid action. Marking subtask attempt as failed.")
                        # Update task status to failed FOR THIS ATTEMPT
                        self.task_manager.update_subtask_status(current_task_index, "failed", error="LLM failed to determine a valid action.")
                        logger.warning("[DEBUG_AGENT] Adding delay after LLM action determination failure...")
                        time.sleep(random.uniform(2.0, 4.0))
                        break # Break INNER loop, proceed to next outer iteration (will retry or fail permanently)

                    # --- Action Execution or Vision Request Handling ---
                    execution_result = self._execute_action(action_decision, current_task)
                    action_type = action_decision.get("action")
                    last_selector_used = action_decision.get("parameters", {}).get("selector") # Store selector used

                    # --- Handle Vision Request by LLM ---
                    if execution_result.get("needs_vision_retry"):
                        vision_request_count += 1
                        # Check if LLM is stuck requesting vision
                        if vision_request_count > MAX_VISION_REQUESTS_PER_ATTEMPT:
                            logger.warning(f"Max vision requests ({MAX_VISION_REQUESTS_PER_ATTEMPT}) reached for task attempt {current_task['attempts']}. Forcing failure.")
                            # Mark task as failed for this attempt
                            self.task_manager.update_subtask_status(current_task_index, "failed", error=f"LLM repeatedly requested vision analysis (>{MAX_VISION_REQUESTS_PER_ATTEMPT} times) without acting.")
                            break # Break INNER loop

                        logger.info("LLM requested vision analysis. Performing analysis...")
                        self._add_to_history("Vision Analysis Requested", {"reason": execution_result.get("reasoning")})

                        if screenshot_bytes:
                            # Perform the vision analysis using the most recent screenshot
                            screenshot_analysis = self.vision_processor.analyze_screenshot_for_action(
                                screenshot_bytes, current_task['description'] # Provide task context, no failure context needed here
                            )
                            analysis_snippet = screenshot_analysis[:200] + ('...' if len(screenshot_analysis)>200 else '')
                            self._add_to_history("Vision Analysis Result (Requested)", analysis_snippet)
                            vision_requested_this_attempt = True # Set flag
                            logger.info("Vision analysis complete. Re-prompting LLM with visual context.")
                            # Loop back to the start of the inner loop (_determine_next_action) with new context
                            continue # Continue the INNER loop
                        else:
                            logger.warning("LLM requested vision analysis, but no screenshot was available.")
                            # Treat this as a failure for the current attempt
                            self.task_manager.update_subtask_status(current_task_index, "failed", error="Vision requested but screenshot failed.")
                            break # Break INNER loop

                    # --- Handle BROWSER Action Results (if not a vision request) ---
                    # This block is reached only if the action was NOT 'request_vision_analysis'
                    update_result = execution_result.get("data", execution_result["message"])
                    update_error = None if execution_result["success"] else execution_result["message"]
                    new_status = "unknown" # Placeholder for status update

                    if execution_result["success"]:
                        # Handle successful actions that terminate the goal or subtask
                        if action_type == "final_answer":
                            final_data = execution_result.get('data', "Goal achieved.")
                            logger.info(f"Goal Achieved! Final Answer: {str(final_data)[:500]}...")
                            new_status = "done"
                            run_status["success"] = True
                            run_status["final_answer"] = final_data
                            run_status["message"] = f"✅ Agent finished successfully! Final Answer provided."
                            self.task_manager.update_subtask_status(current_task_index, new_status, result=update_result, error=update_error)
                            iteration_count = self.max_iterations # Force outer loop break
                            break # Break INNER loop
                        elif action_type == "save_json":
                            file_path = execution_result.get("data", {}).get("file_path", "N/A")
                            logger.info(f"Goal Achieved! Data saved to file: {file_path}")
                            new_status = "done"
                            run_status["success"] = True
                            run_status["output_file"] = file_path
                            run_status["message"] = f"✅ Agent finished successfully! Data saved to: {file_path}"
                            self.task_manager.update_subtask_status(current_task_index, new_status, result=update_result, error=update_error)
                            iteration_count = self.max_iterations # Force outer loop break
                            break # Break INNER loop
                        elif action_type == "subtask_complete":
                            subtask_data = execution_result.get('data', "Subtask completed.")
                            logger.info(f"Subtask marked complete by LLM. Result: {str(subtask_data)[:500]}...")
                            new_status = "done"
                            self.task_manager.update_subtask_status(current_task_index, new_status, result=update_result, error=update_error)
                            break # Break INNER loop - this subtask is done
                        else: # Other successful browser actions (navigate, click, type, extract_*, scroll)
                            logger.info(f"Action '{action_type}' executed successfully for subtask {current_task_index + 1}.")
                            # Assume successful action completes the subtask unless LLM specifies otherwise later
                            new_status = "done"
                            self.task_manager.update_subtask_status(current_task_index, new_status, result=update_result, error=update_error)
                            # --- Implicit Completion Check (Optional) ---
                            # if self._check_and_mark_implicit_completion(current_task_index, action_type, action_decision.get("parameters", {})):
                            #      logger.info("Implicitly completed next task.")
                            # --- End Implicit Check ---
                            break # Break INNER loop - this subtask is considered done
                    else: # Execution failed OR action was 'subtask_failed'
                        error_msg = execution_result["message"]
                        logger.warning(f"Action '{action_type}' failed for subtask #{current_task_index + 1}. Error: {error_msg}")
                        # Mark the current ATTEMPT as failed
                        new_status = "failed"
                        if last_selector_used:
                            # Record the selector that failed for potential use in retry analysis
                            self.task_manager.subtasks[current_task_index]['last_failed_selector'] = last_selector_used
                            logger.debug(f"[DEBUG_AGENT] Recorded last failed selector for task {current_task_index+1}: {last_selector_used}")
                        self.task_manager.update_subtask_status(current_task_index, new_status, result=update_result, error=update_error) # Pass error message
                        logger.warning("[DEBUG_AGENT] Adding delay after action execution failure...")
                        time.sleep(random.uniform(1.5, 3.0)) # Pause after failure
                        break # Break INNER loop (will retry in next outer iteration if attempts remain)

                # --- End of Inner Loop for the current task attempt ---
                # Control flow continues to the next iteration of the OUTER loop
                # This will either get the next pending task or retry the current one if it failed and has attempts left.

            # --- OUTER Loop End Handling ---
            if not run_status["success"]: # Check if goal wasn't achieved
                if iteration_count >= self.max_iterations and not self.task_manager.is_complete():
                    run_status["message"] = f"⚠️ Agent stopped: Maximum iterations ({self.max_iterations}) reached before completing all tasks."
                    logger.warning(run_status["message"])
                elif self.task_manager.is_complete(): # All tasks processed, but no final success action
                    # Message might have already been set if tasks finished without final action confirmed
                    if run_status["message"] == "Agent stopped.": # Default message
                        run_status["message"] = f"🏁 Agent finished all tasks, but the final goal state (save_json or final_answer) was not reached."
                    logger.warning(run_status["message"])
                # else: Task loop ended unexpectedly (already handled when current_task is None)


            # Final status printing, summary, etc.
            if run_status["final_answer"]:
                print(f"\n✅ Final Answer:\n{run_status['final_answer']}")
            elif run_status["output_file"]:
                print(f"\n✅ Output File: {run_status['output_file']}")
            else:
                print(f"\n{run_status['message']}") # Print the determined final status message

            print("\n📋 Final Task Summary:")
            print(self.task_manager.get_progress_summary())
            # Show extracted data if no file was saved but data exists
            if not self.output_file_path and self.extracted_data_history:
                print("\n📋 Extracted Data Summary (not saved to file):")
                print(self._get_extracted_data_summary())


        except Exception as e:
            logger.critical(f"An critical unexpected error occurred during the agent run: {e}", exc_info=True)
            run_status["success"] = False
            run_status["message"] = f"❌ Critical Error during agent run: {e}"
        finally:
            logger.info("--- Ending Agent Run ---")
            cleanup_start = time.time()
            logger.debug("[DEBUG_AGENT] Closing browser controller...")
            self.browser_controller.close()
            cleanup_end = time.time()
            logger.info(f"Browser cleanup took {cleanup_end - cleanup_start:.2f}s")

            end_time = time.time()
            run_status["duration_seconds"] = round(end_time - start_time, 2)
            # Get final summary AFTER potential critical errors
            try:
                run_status["task_summary"] = self.task_manager.get_progress_summary()
            except Exception as summary_e:
                logger.error(f"Failed to get final task summary: {summary_e}")
                run_status["task_summary"] = "Error retrieving final summary."

            logger.info(f"Agent run finished in {run_status['duration_seconds']:.2f} seconds.")
            logger.info(f"Final Run Status: {run_status}")
            # Log final summary at INFO level for visibility
            logger.info(f"Final Task Breakdown:\n{run_status['task_summary']}")


        return run_status # Return the status dictionary