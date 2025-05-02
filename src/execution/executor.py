# /src/executor.py
import json
import logging
import time
import os
from patchright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError, expect
from typing import Optional, Dict, Any, Tuple, List
from pydantic import BaseModel, Field
import re
from PIL import Image
from pixelmatch.contrib.PIL import pixelmatch
import io

from ..browser.browser_controller import BrowserController # Re-use for browser setup/teardown
from ..llm.llm_client import LLMClient
from ..agents.recorder_agent import WebAgent
from ..utils.image_utils import compare_images

# Define a short timeout specifically for selector validation during healing
HEALING_SELECTOR_VALIDATION_TIMEOUT_MS = 2000


class HealingSelectorSuggestion(BaseModel):
    """Schema for the LLM's suggested replacement selector during healing."""
    new_selector: Optional[str] = Field(None, description="The best suggested alternative CSS selector based on visual and DOM context, or null if no suitable alternative is found.")
    reasoning: str = Field(..., description="Explanation for the suggested selector choice or the reason why healing could not determine a better selector.")

logger = logging.getLogger(__name__)

class TestExecutor:
    """
    Executes a recorded test case from a JSON file deterministically using Playwright.
    """

    def __init__(self, 
            llm_client: Optional[LLMClient], 
            headless: bool = True, 
            default_timeout: int = 5000,    # Default timeout for actions/assertions
            enable_healing: bool = False,   # Flag for healing
            healing_mode: str = 'soft',     # Healing mode ('soft' or 'hard')
            healing_retries: int = 1,        # Max soft healing attempts per step
            baseline_dir: str = "./visual_baselines", # Add baseline dir
            pixel_threshold: float = 0.01, # Default 1% pixel difference threshold
            get_performance: bool = False,
            get_network_requests: bool = False
        ): 
        self.headless = headless
        self.default_timeout = default_timeout # Milliseconds
        self.llm_client = llm_client
        self.browser_controller: Optional[BrowserController] = None
        self.page: Optional[Page] = None
        self.enable_healing = enable_healing
        self.healing_mode = healing_mode
        self.healing_retries_per_step = healing_retries
        self.healing_attempts_log: List[Dict] = [] # To store healing attempts info
        self.get_performance = get_performance
        self.get_network_requests = get_network_requests
        
        
        logger.info(f"TestExecutor initialized (headless={headless}, timeout={default_timeout}ms).")
        log_message = ""
        if self.enable_healing:
            log_message += f" with Healing ENABLED (mode={self.healing_mode}, retries={self.healing_retries_per_step})"
            if not self.llm_client:
                 logger.warning("Self-healing enabled, but LLMClient not provided. Healing will not function.")
            else:
                 log_message += f" using LLM provider '{self.llm_client.provider}'."
        else:
            log_message += "."
        logger.info(log_message)

        if not self.llm_client and not headless: # Vision verification needs LLM
             logger.warning("TestExecutor initialized without LLMClient. Vision-based assertions ('assert_passed_verification') will fail.")
        elif self.llm_client:
             logger.info(f"TestExecutor initialized (headless={headless}, timeout={default_timeout}ms) with LLMClient for provider '{self.llm_client.provider}'.")
        else:
             logger.info(f"TestExecutor initialized (headless={headless}, timeout={default_timeout}ms). LLMClient not provided (headless mode or vision assertions not needed).")
        
        self.baseline_dir = os.path.abspath(baseline_dir)
        self.pixel_threshold = pixel_threshold # Store threshold
        logger.info(f"TestExecutor initialized (visual baseline dir: {self.baseline_dir}, pixel threshold: {self.pixel_threshold*100:.2f}%)")
        os.makedirs(self.baseline_dir, exist_ok=True) # Ensure baseline dir exists
    
    
    def _get_locator(self, selector: str):
        """Helper to get a Playwright locator, handling potential errors."""
        if not self.page:
            raise PlaywrightError("Page is not initialized.")
        if not selector:
            raise ValueError("Selector cannot be empty.")
        
        is_likely_xpath = selector.startswith(('/', '(', '//')) or \
                          ('/' in selector and not any(c in selector for c in ['#', '.', '[', '>', '+', '~']))

        # If it looks like XPath but doesn't have a prefix, add 'css='
        # Playwright's locator treats "css=<xpath>" as an XPath selector.
        processed_selector = selector
        if is_likely_xpath and not selector.startswith(('css=', 'xpath=')):
            logger.warning(f"Selector '{selector}' looks like XPath but lacks prefix. Assuming XPath and adding 'css=' prefix.")
            processed_selector = f"xpath={selector}"
        
        try:
            logger.debug(f"Attempting to locate using: '{processed_selector}'")
            return self.page.locator(processed_selector).first
        except Exception as e:
            # Catch errors during locator creation itself (e.g., invalid selector syntax)
            logger.error(f"Failed to create locator for processed selector: '{processed_selector}'. Original: '{selector}'. Error: {e}")
            # Re-raise using the processed selector in the message for clarity
            raise PlaywrightError(f"Invalid selector syntax or error creating locator: '{processed_selector}'. Error: {e}") from e
    
        
    def _load_baseline(self, baseline_id: str) -> Tuple[Optional[Image.Image], Optional[Dict]]:
        """Loads the baseline image and metadata."""
        metadata_path = os.path.join(self.baseline_dir, f"{baseline_id}.json")
        image_path = os.path.join(self.baseline_dir, f"{baseline_id}.png") # Assume PNG

        if not os.path.exists(metadata_path) or not os.path.exists(image_path):
            logger.error(f"Baseline files not found for ID '{baseline_id}' in {self.baseline_dir}")
            return None, None

        try:
            with open(metadata_path, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
            baseline_img = Image.open(image_path).convert("RGBA") # Load and ensure RGBA
            logger.info(f"Loaded baseline '{baseline_id}' (Image: {image_path}, Metadata: {metadata_path})")
            return baseline_img, metadata
        except Exception as e:
            logger.error(f"Error loading baseline files for ID '{baseline_id}': {e}", exc_info=True)
            return None, None

    def _attempt_soft_healing(
            self,
            failed_step: Dict[str, Any],
            failed_selector: Optional[str],
            error_message: str
        ) -> Tuple[bool, Optional[str], str]:
        """
        Attempts to find a new selector using the LLM based on the failed step's context and validate it.

        Returns:
            Tuple[bool, Optional[str], str]: (healing_success, new_selector, reasoning)
        """
        if not self.llm_client:
            logger.error("Soft Healing: LLMClient not available.")
            return False, None, "LLMClient not configured for healing."
        if not self.browser_controller or not self.page:
             logger.error("Soft Healing: BrowserController or Page not available.")
             return False, None, "Browser state unavailable for healing."

        logger.info(f"Soft Healing: Gathering context for step {failed_step.get('step_id')}")

        try:
            current_url = self.browser_controller.get_current_url()
            screenshot_bytes = self.browser_controller.take_screenshot()
            dom_state = self.browser_controller.get_structured_dom(highlight_all_clickable_elements=False, viewport_expansion=-1)
            dom_context_str = "DOM context could not be retrieved."
            if dom_state and dom_state.element_tree:
                dom_context_str, _ = dom_state.element_tree.generate_llm_context_string(context_purpose='verification')
            else:
                 logger.warning("Soft Healing: Failed to get valid DOM state.")

            if not screenshot_bytes:
                 logger.error("Soft Healing: Failed to capture screenshot.")
                 return False, None, "Failed to capture screenshot for context."

        except Exception as e:
            logger.error(f"Soft Healing: Error gathering context: {e}", exc_info=True)
            return False, None, f"Error gathering context: {e}"

        # Construct the prompt
        prompt = f"""You are an AI Test Self-Healing Assistant. A step in an automated test failed, likely due to an incorrect or outdated CSS selector. Your goal is to analyze the current page state and suggest a more robust replacement selector for the intended element.

**Failed Test Step Information:**
- Step Description: "{failed_step.get('description', 'N/A')}"
- Original Action: "{failed_step.get('action', 'N/A')}"
- Failed Selector: `{failed_selector or 'N/A'}`
- Error Message: "{error_message}"

**Current Page State:**
- URL: {current_url}
- Attached Screenshot: Analyze the visual layout to identify the target element corresponding to the step description.
- HTML Context (Visible elements, interactive `[index]`, static `(Static)`):
```html
{dom_context_str}
```

**Your Task:**
1. Based on the step description, the original action, the visual screenshot, AND the HTML context, identify the element the test likely intended to interact with.
2. Suggest a **single, robust CSS selector** for this element using **NATIVE attributes** (like `id`, `name`, `data-testid`, `data-cy`, `aria-label`, `placeholder`, unique visible text combined with tag, stable class combinations).
3. **CRITICAL: Do NOT suggest selectors based on `data-ai-id` or unstable attributes (e.g., dynamic classes, complex positional selectors like :nth-child unless absolutely necessary and combined with other stable attributes).**
4. Prioritize standard, semantic, and test-specific attributes (`id`, `data-testid`, `name`).
5. If you cannot confidently identify the intended element or find a robust selector, return `null` for `new_selector`.

**Output Format:** Respond ONLY with a JSON object matching the following schema:
```json
{{
  "new_selector": "YOUR_SUGGESTED_CSS_SELECTOR_OR_NULL",
  "reasoning": "Explain your choice of selector, referencing visual cues, HTML attributes, and the original step description. If returning null, explain why."
}}
```
"""

        try:
            logger.info("Soft Healing: Requesting selector suggestion from LLM...")
            response_obj = self.llm_client.generate_json(
                HealingSelectorSuggestion,
                prompt,
                image_bytes=screenshot_bytes
            )

            if isinstance(response_obj, HealingSelectorSuggestion):
                if response_obj.new_selector:
                    suggested_selector = response_obj.new_selector
                    logger.info(f"Soft Healing: LLM suggested new selector: '{response_obj.new_selector}'. Reasoning: {response_obj.reasoning}")
                    logger.info(f"Soft Healing: Validating suggested selector '{suggested_selector}'...")
                    validation_passed = False
                    validation_reasoning_suffix = ""
                    try:
                        # Use page.locator() with a short timeout for existence check
                        count = self.page.locator(suggested_selector).count()

                        if count > 0:
                            validation_passed = True
                            logger.info(f"Soft Healing: Validation PASSED. Selector '{suggested_selector}' found {count} element(s).")
                            if count > 1:
                                logger.warning(f"Soft Healing: Suggested selector '{suggested_selector}' found {count} elements (expected 1). Will target the first.")
                        else: # count == 0
                            logger.warning(f"Soft Healing: Validation FAILED. Selector '{suggested_selector}' found 0 elements within {HEALING_SELECTOR_VALIDATION_TIMEOUT_MS}ms.")
                            validation_reasoning_suffix = " [Validation Failed: Selector found 0 elements]"

                    except PlaywrightTimeoutError:
                         logger.warning(f"Soft Healing: Validation TIMEOUT ({HEALING_SELECTOR_VALIDATION_TIMEOUT_MS}ms) checking selector '{suggested_selector}'.")
                         validation_reasoning_suffix = f" [Validation Failed: Timeout after {HEALING_SELECTOR_VALIDATION_TIMEOUT_MS}ms]"
                    except PlaywrightError as e: # Catch invalid selector syntax errors
                         logger.warning(f"Soft Healing: Validation FAILED. Invalid selector syntax for '{suggested_selector}'. Error: {e}")
                         validation_reasoning_suffix = f" [Validation Failed: Invalid selector syntax - {e}]"
                    except Exception as e:
                         logger.error(f"Soft Healing: Unexpected error during selector validation for '{suggested_selector}': {e}", exc_info=True)
                         validation_reasoning_suffix = f" [Validation Error: {type(e).__name__}]"
                    # --- End Validation Step ---

                    # Return success only if validation passed
                    if validation_passed:
                        return True, suggested_selector, response_obj.reasoning
                    else:
                        # Update reasoning with validation failure details
                        return False, None, response_obj.reasoning + validation_reasoning_suffix


                else:
                    logger.warning(f"Soft Healing: LLM could not suggest a new selector. Reasoning: {response_obj.reasoning}")
                    return False, None, response_obj.reasoning
            elif isinstance(response_obj, str): # LLM returned an error string
                 logger.error(f"Soft Healing: LLM returned an error: {response_obj}")
                 return False, None, f"LLM Error: {response_obj}"
            else: # Unexpected response type
                 logger.error(f"Soft Healing: Unexpected response type from LLM: {type(response_obj)}")
                 return False, None, f"Unexpected LLM response type: {type(response_obj)}"

        except Exception as llm_e:
            logger.error(f"Soft Healing: Error during LLM communication: {llm_e}", exc_info=True)
            return False, None, f"LLM communication error: {llm_e}"
        
    def _trigger_hard_healing(self, feature_description: str, original_file_path: str) -> None:
        """
        Closes the current browser and triggers the WebAgent to re-record the test.
        """
        logger.warning("--- Triggering Hard Healing (Re-Recording) ---")
        if not feature_description:
            logger.error("Hard Healing: Cannot re-record without the original feature description.")
            return
        if not self.llm_client:
            logger.error("Hard Healing: Cannot re-record without an LLMClient.")
            return

        # 1. Close current browser
        try:
            if self.browser_controller:
                self.browser_controller.close()
                self.browser_controller = None
                self.page = None
                logger.info("Hard Healing: Closed executor browser.")
        except Exception as close_err:
            logger.error(f"Hard Healing: Error closing executor browser: {close_err}")
            # Continue anyway, try to re-record

        # 2. Instantiate Recorder Agent
        #    NOTE: Assume re-recording is automated. Add flag if interactive needed.
        try:
            logger.info("Hard Healing: Initializing WebAgent for automated re-recording...")
            # Use the existing LLM client
            recorder_agent = WebAgent(
                llm_client=self.llm_client,
                headless=False,  # Re-recording needs visible browser initially
                is_recorder_mode=True,
                automated_mode=True, # Run re-recording automatically
                # Pass original filename stem to maybe overwrite or create variant
                filename=os.path.splitext(os.path.basename(original_file_path))[0] + "_healed_"
            )

            # 3. Run Recorder
            logger.info(f"Hard Healing: Starting re-recording for feature: '{feature_description}'")
            recording_result = recorder_agent.record(feature_description)

            # 4. Log Outcome
            if recording_result.get("success"):
                logger.info(f"✅ Hard Healing: Re-recording successful. New test file saved to: {recording_result.get('output_file')}")
            else:
                logger.error(f"❌ Hard Healing: Re-recording FAILED. Message: {recording_result.get('message')}")

        except Exception as record_err:
            logger.critical(f"❌ Hard Healing: Critical error during re-recording setup or execution: {record_err}", exc_info=True)
   

    def run_test(self, json_file_path: str) -> Dict[str, Any]:
        """Loads and executes the test steps from the JSON file."""
        start_time = time.time()
        self.healing_attempts_log = [] # Reset log for this run

        any_step_successfully_healed = False
        
        run_status = {
            "test_file": json_file_path,
            "status": "FAIL", # Default to fail
            "message": "Execution initiated.",
            "steps_executed": 0,
            "failed_step": None,
            "error_details": None,
            "screenshot_on_failure": None,
            "console_messages_on_failure": [],
            "all_console_messages": [],
            "performance_timing": None,
            "network_requests": [],
            "duration_seconds": 0.0,
            "healing_enabled": self.enable_healing,
            "healing_mode": self.healing_mode if self.enable_healing else "disabled",
            "healing_attempts": self.healing_attempts_log, # Reference the list
            "healed_file_saved": False,
            "healed_steps_count": 0,
            "visual_assertion_results": []
        }

        try:
            # --- Load Test Data ---
            logger.info(f"Loading test case from: {json_file_path}")
            if not os.path.exists(json_file_path):
                 raise FileNotFoundError(f"Test file not found: {json_file_path}")
            with open(json_file_path, 'r', encoding='utf-8') as f:
                test_data = json.load(f)
                modified_test_data = test_data.copy() 

            steps = modified_test_data.get("steps", [])
            viewport = next((json.load(open(os.path.join(self.baseline_dir, f"{step.get('parameters', {}).get('baseline_id')}.json"))).get("viewport_size") for step in steps if step.get("action") == "assert_visual_match" and step.get('parameters', {}).get('baseline_id') and os.path.exists(os.path.join(self.baseline_dir, f"{step.get('parameters', {}).get('baseline_id')}.json"))), None)
            test_name = modified_test_data.get("test_name", "Unnamed Test")
            feature_description = modified_test_data.get("feature_description", "")
            first_navigation_done = False
            run_status["test_name"] = test_name
            logger.info(f"Executing test: '{test_name}' with {len(steps)} steps.")

            if not steps:
                raise ValueError("No steps found in the test file.")

            # --- Setup Browser ---
            self.browser_controller = BrowserController(headless=self.headless, viewport_size=viewport)
            # Set default timeout before starting the page
            self.browser_controller.default_action_timeout = self.default_timeout
            self.browser_controller.default_navigation_timeout = max(self.default_timeout, 30000) # Ensure navigation timeout is reasonable
            self.browser_controller.start()
            self.page = self.browser_controller.page
            if not self.page:
                 raise PlaywrightError("Failed to initialize browser page.")
            # Re-apply default timeout to the page context AFTER it's created
            self.page.set_default_timeout(self.default_timeout)
            logger.info(f"Browser page initialized with default action timeout: {self.default_timeout}ms")
            
            self.browser_controller.clear_console_messages()
            self.browser_controller.clear_network_requests() 

            # --- Execute Steps ---
            for i, step in enumerate(steps):
                step_id = step.get("step_id", i + 1)
                action = step.get("action")
                original_selector = step.get("selector")
                params = step.get("parameters", {})
                description = step.get("description", f"Step {step_id}")
                wait_after = step.get("wait_after_secs", 0) # Get wait time

                run_status["steps_executed"] = i + 1 # Track steps attempted
                logger.info(f"--- Executing Step {step_id}: {action} - {description} ---")
                if original_selector: logger.info(f"Original Selector: {original_selector}")
                if params: logger.info(f"Parameters: {params}")

                # --- Healing Loop ---
                step_healed = False
                current_healing_attempts = 0
                current_selector = original_selector # Start with the recorded selector
                last_error = None # Store the last error encountered
                successful_healed_selector_for_step = None
                run_status["visual_assertion_results"] = []
                while not step_healed and current_healing_attempts <= self.healing_retries_per_step:
                    try:
                        if action == "navigate":
                            url = params.get("url")
                            if not url: raise ValueError("Missing 'url' parameter for navigate.")
                            self.browser_controller.goto(url)# Uses default navigation timeout from context
                            if not first_navigation_done:
                                if self.get_performance:
                                    run_status["performance_timing"] = self.browser_controller.page_performance_timing
                                first_navigation_done = True
                        elif action == "click":
                            if not current_selector: raise ValueError("Missing 'current_selector' for click.")
                            locator = self._get_locator(current_selector)
                            locator.click(timeout=self.default_timeout) # Explicit timeout for action
                        elif action == "type":
                            text = params.get("text")
                            if not current_selector: raise ValueError("Missing 'current_selector' for type.")
                            if text is None: raise ValueError("Missing 'text' parameter for type.")
                            locator = self._get_locator(current_selector)
                            locator.fill(text, timeout=self.default_timeout) # Use fill for robustness
                        elif action == "scroll": # Less common, but support if recorded
                            direction = params.get("direction")
                            if direction not in ["up", "down"]: raise ValueError("Invalid 'direction'.")
                            amount = "window.innerHeight" if direction=="down" else "-window.innerHeight"
                            self.page.evaluate(f"window.scrollBy(0, {amount})")
                        elif action == "check": 
                            if not current_selector: raise ValueError("Missing 'current_selector' for check action.")
                            # Use the browser_controller method which handles locator/timeout
                            self.browser_controller.check(current_selector)
                        elif action == "uncheck":
                            if not current_selector: raise ValueError("Missing 'current_selector' for uncheck action.")
                            # Use the browser_controller method
                            self.browser_controller.uncheck(current_selector)
                        elif action == "select":
                            option_label = params.get("option_label")
                            option_value = params.get("option_value") # Support value too if recorded
                            option_index_str = params.get("option_index") # Support index if recorded
                            option_param = None
                            param_type = None

                            if option_label is not None:
                                option_param = {"label": option_label}
                                param_type = f"label '{option_label}'"
                            elif option_value is not None:
                                option_param = {"value": option_value}
                                param_type = f"value '{option_value}'"
                            elif option_index_str is not None and option_index_str.isdigit():
                                option_param = {"index": int(option_index_str)}
                                param_type = f"index {option_index_str}"
                            else:
                                raise ValueError("Missing 'option_label', 'option_value', or 'option_index' parameter for select action.")

                            if not current_selector: raise ValueError("Missing 'current_selector' for select action.")

                            logger.info(f"Selecting option by {param_type} in element: {current_selector}")
                            locator = self._get_locator(current_selector)
                            locator.select_option(**option_param, timeout=self.default_timeout)
                        elif action == "wait_for_load_state":
                            state = params.get("state", "load")
                            self.page.wait_for_load_state(state, timeout=self.browser_controller.default_navigation_timeout) # Use navigation timeout
                        elif action == "wait_for_selector": # Explicit wait
                            wait_state = params.get("state", "visible")
                            timeout = params.get("timeout_ms", self.default_timeout)
                            if not current_selector: raise ValueError("Missing 'current_selector' for wait_for_selector.")
                            locator = self._get_locator(current_selector)
                            locator.wait_for(state=wait_state, timeout=timeout)
                        # --- Assertions ---
                        elif action == "assert_text_contains":
                            expected_text = params.get("expected_text")
                            if not current_selector: raise ValueError("Missing 'current_selector' for assertion.")
                            if expected_text is None: raise ValueError("Missing 'expected_text'.")
                            locator = self._get_locator(current_selector)
                            expect(locator).to_contain_text(expected_text, timeout=self.default_timeout)
                        elif action == "assert_text_equals":
                            expected_text = params.get("expected_text")
                            if not current_selector: raise ValueError("Missing 'current_selector' for assertion.")
                            if expected_text is None: raise ValueError("Missing 'expected_text'.")
                            locator = self._get_locator(current_selector)
                            expect(locator).to_have_text(expected_text, timeout=self.default_timeout)
                        elif action == "assert_visible":
                            if not current_selector: raise ValueError("Missing 'current_selector' for assertion.")
                            locator = self._get_locator(current_selector)
                            expect(locator).to_be_visible(timeout=self.default_timeout)
                        elif action == "assert_hidden":
                            if not current_selector: raise ValueError("Missing 'current_selector' for assertion.")
                            locator = self._get_locator(current_selector)
                            expect(locator).to_be_hidden(timeout=self.default_timeout)
                        elif action == "assert_attribute_equals":
                            attr_name = params.get("attribute_name")
                            expected_value = params.get("expected_value")
                            if not current_selector: raise ValueError("Missing 'current_selector' for assertion.")
                            if not attr_name: raise ValueError("Missing 'attribute_name'.")
                            if expected_value is None: raise ValueError("Missing 'expected_value'.")
                            locator = self._get_locator(current_selector)
                            expect(locator).to_have_attribute(attr_name, expected_value, timeout=self.default_timeout)
                        elif action == "assert_element_count":
                            expected_count = params.get("expected_count")
                            if not current_selector: raise ValueError("Missing 'current_selector' for assertion.")
                            if expected_count is None: raise ValueError("Missing 'expected_count'.")
                            if not isinstance(expected_count, int): raise ValueError("'expected_count' must be an integer.") # Add type check

                            # --- FIX: Get locator for count without using .first ---
                            # Apply the same current_selector processing as in _get_locator if needed
                            is_likely_xpath = current_selector.startswith(('/', '(', '//')) or \
                                            ('/' in current_selector and not any(c in current_selector for c in ['#', '.', '[', '>', '+', '~']))
                            processed_selector = current_selector
                            if is_likely_xpath and not current_selector.startswith(('css=', 'xpath=')):
                                processed_selector = f"xpath={current_selector}"

                            # Get the locator for potentially MULTIPLE elements
                            count_locator = self.page.locator(processed_selector)
                            # --- End FIX ---

                            logger.info(f"Asserting count of elements matching '{processed_selector}' to be {expected_count}")
                            expect(count_locator).to_have_count(expected_count, timeout=self.default_timeout)
                        elif action == "assert_checked":
                            if not current_selector: raise ValueError("Missing 'current_selector' for assert_checked.")
                            locator = self._get_locator(current_selector)
                            # Use Playwright's dedicated assertion for checked state
                            expect(locator).to_be_checked(timeout=self.default_timeout)
                        elif action == "assert_not_checked":
                            if not current_selector: raise ValueError("Missing 'current_selector' for assert_not_checked.")
                            locator = self._get_locator(current_selector)
                            # Use .not modifier with the checked assertion
                            expect(locator).not_to_be_checked(timeout=self.default_timeout)
                        elif action == "assert_disabled":
                            if not current_selector: raise ValueError("Missing 'current_selector' for assert_disabled.")
                            locator = self._get_locator(current_selector)
                            # Use Playwright's dedicated assertion for disabled state
                            expect(locator).to_be_disabled(timeout=self.default_timeout)
                        elif action == "assert_enabled":
                            if not current_selector: raise ValueError("Missing 'current_selector' for assert_enabled.")
                            locator = self._get_locator(current_selector)
                            expect(locator).to_be_enabled(timeout=self.default_timeout)
                        elif action == "assert_visual_match":
                            baseline_id = params.get("baseline_id")
                            element_selector = step.get("selector") # Use step's selector if available
                            use_llm = params.get("use_llm_fallback", True)
                            # Allow overriding threshold per step
                            step_threshold = params.get("pixel_threshold", self.pixel_threshold)

                            if not baseline_id:
                                raise ValueError("Missing 'baseline_id' parameter for assert_visual_match.")

                            logger.info(f"--- Performing Visual Assertion: '{baseline_id}' (Selector: {element_selector}, Threshold: {step_threshold*100:.2f}%, LLM: {use_llm}) ---")

                            # 1. Load Baseline
                            baseline_img, baseline_meta = self._load_baseline(baseline_id)
                            if not baseline_img or not baseline_meta:
                                raise FileNotFoundError(f"Baseline '{baseline_id}' not found or failed to load.")

                            # 2. Capture Current State
                            current_screenshot_bytes = None
                            if element_selector:
                                current_screenshot_bytes = self.browser_controller.take_screenshot_element(element_selector)
                            else:
                                current_screenshot_bytes = self.browser_controller.take_screenshot() # Full page

                            if not current_screenshot_bytes:
                                raise PlaywrightError("Failed to capture current screenshot for visual comparison.")

                            try:
                                # Create a BytesIO buffer to treat the bytes like a file
                                buffer = io.BytesIO(current_screenshot_bytes)
                                # Open the image from the buffer using Pillow
                                img = Image.open(buffer)
                                # Ensure the image is in RGBA format for consistency,
                                # especially important for pixel comparisons that might expect an alpha channel.
                                logger.info("received")
                                current_img = img.convert("RGBA")
                            except Exception as e:
                                logger.error(f"Failed to convert bytes to PIL Image: {e}", exc_info=True)
                                current_img = None

                            
                            
                            if not current_img:
                                raise RuntimeError("Failed to process current screenshot bytes into an image.")
                            

                            # 3. Pre-check Dimensions
                            if baseline_img.size != current_img.size:
                                size_mismatch_msg = f"Visual Assertion Failed: Image dimensions mismatch for '{baseline_id}'. Baseline: {baseline_img.size}, Current: {current_img.size}."
                                logger.error(size_mismatch_msg)
                                # Save current image for debugging
                                ts = time.strftime("%Y%m%d_%H%M%S")
                                current_img_path = os.path.join("output", f"visual_fail_{baseline_id}_current_{ts}.png")
                                current_img.save(current_img_path)
                                logger.info(f"Saved current image (dimension mismatch) to: {current_img_path}")
                                raise AssertionError(size_mismatch_msg) # Fail the assertion

                            # 4. Pixel Comparison
                            img_diff = Image.new("RGBA", baseline_img.size) # Image to store diff pixels
                            try:
                                mismatched_pixels = pixelmatch(baseline_img, current_img, img_diff, includeAA=True, threshold=0.1) # Use default pixelmatch threshold first
                            except Exception as pm_error:
                                logger.error(f"Error during pixelmatch comparison for '{baseline_id}': {pm_error}", exc_info=True)
                                raise RuntimeError(f"Pixelmatch library error: {pm_error}") from pm_error


                            total_pixels = baseline_img.width * baseline_img.height
                            diff_ratio = mismatched_pixels / total_pixels if total_pixels > 0 else 0
                            logger.info(f"Pixel comparison for '{baseline_id}': Mismatched Pixels = {mismatched_pixels}, Total Pixels = {total_pixels}, Difference = {diff_ratio*100:.4f}%")

                            # 5. Check against threshold
                            pixel_match_passed = diff_ratio <= step_threshold
                            llm_reasoning = None
                            diff_image_path = None

                            if pixel_match_passed:
                                logger.info(f"✅ Visual Assertion PASSED (Pixel Diff <= Threshold) for '{baseline_id}'.")
                                # Step completed successfully
                            else:
                                logger.warning(f"Visual Assertion: Pixel difference ({diff_ratio*100:.4f}%) exceeds threshold ({step_threshold*100:.2f}%) for '{baseline_id}'.")

                                # Save diff image regardless of LLM outcome
                                ts = time.strftime("%Y%m%d_%H%M%S")
                                diff_image_path = os.path.join("output", f"visual_diff_{baseline_id}_{ts}.png")
                                try:
                                    img_diff.save(diff_image_path)
                                    logger.info(f"Saved pixel difference image to: {diff_image_path}")
                                except Exception as save_err:
                                    logger.error(f"Failed to save diff image: {save_err}")
                                    diff_image_path = None # Mark as failed

                                # 6. LLM Fallback
                                if use_llm and self.llm_client:
                                    logger.info(f"Attempting LLM visual comparison fallback for '{baseline_id}'...")
                                    baseline_bytes = io.BytesIO()
                                    baseline_img.save(baseline_bytes, format='PNG')
                                    baseline_bytes = baseline_bytes.getvalue()

                                    # --- UPDATED LLM PROMPT for Stitched Image ---
                                    llm_prompt = f"""Analyze the combined image provided below for the purpose of automated software testing.
            The LEFT half (labeled '1: Baseline') is the established baseline screenshot.
            The RIGHT half (labeled '2: Current') is the current state screenshot.

            Compare these two halves to determine if they are SEMANTICALLY equivalent from a user's perspective.

            IGNORE minor differences like:
            - Anti-aliasing variations
            - Single-pixel shifts
            - Tiny rendering fluctuations
            - Small, insignificant dynamic content changes (e.g., blinking cursors, exact timestamps if not the focus).

            FOCUS ON significant differences like:
            - Layout changes (elements moved, resized, missing, added)
            - Major color changes of key elements
            - Text content changes (errors, different labels, etc.)
            - Missing or fundamentally different images/icons.

            Baseline ID: "{baseline_id}"
            Captured URL (Baseline): "{baseline_meta.get('url_captured', 'N/A')}"
            Selector (Baseline): "{baseline_meta.get('selector_captured', 'Full Page')}"

            Based on these criteria, are the two halves (baseline vs. current) functionally and visually equivalent enough to PASS a visual regression test?

            Respond ONLY with "YES" or "NO", followed by a brief explanation justifying your answer by referencing differences between the left and right halves.
            Example YES: YES - The left (baseline) and right (current) images are visually equivalent. Minor text rendering differences are ignored.
            Example NO: NO - The primary call-to-action button visible on the left (baseline) is missing on the right (current).
            """
                                    # --- END UPDATED PROMPT ---

                                    try:
                                        # No change here, compare_images handles the stitching internally
                                        llm_response = compare_images(llm_prompt, baseline_bytes, current_screenshot_bytes, self.llm_client)
                                        logger.info(f"LLM visual comparison response for '{baseline_id}': {llm_response}")
                                        llm_reasoning = llm_response # Store reasoning

                                        if llm_response.strip().upper().startswith("YES"):
                                            logger.info(f"✅ Visual Assertion PASSED (LLM Override) for '{baseline_id}'.")
                                            pixel_match_passed = True # Override pixel result
                                        elif llm_response.strip().upper().startswith("NO"):
                                            logger.warning(f"Visual Assertion: LLM confirmed significant difference for '{baseline_id}'.")
                                            pixel_match_passed = False # Confirm failure
                                        else:
                                            logger.warning(f"Visual Assertion: LLM response unclear for '{baseline_id}'. Treating as failure.")
                                            pixel_match_passed = False
                                    except Exception as llm_err:
                                        logger.error(f"LLM visual comparison failed: {llm_err}", exc_info=True)
                                        llm_reasoning = f"LLM Error: {llm_err}"
                                        pixel_match_passed = False # Treat LLM error as failure

                                else: # LLM fallback not enabled or LLM not available
                                    logger.warning(f"Visual Assertion: LLM fallback skipped for '{baseline_id}'. Failing based on pixel difference.")
                                    pixel_match_passed = False

                                # 7. Handle Final Failure
                                if not pixel_match_passed:
                                    failure_msg = f"Visual Assertion Failed for '{baseline_id}'. Pixel diff: {diff_ratio*100:.4f}% (Threshold: {step_threshold*100:.2f}%)."
                                    if llm_reasoning: failure_msg += f" LLM Reason: {llm_reasoning}"
                                    logger.error(failure_msg)
                                    # Add details to run_status before raising
                                    visual_failure_details = {
                                        "baseline_id": baseline_id,
                                        "pixel_difference_ratio": diff_ratio,
                                        "pixel_threshold": step_threshold,
                                        "mismatched_pixels": mismatched_pixels,
                                        "diff_image_path": diff_image_path,
                                        "llm_reasoning": llm_reasoning
                                    }
                                    # We need to store this somewhere accessible when raising the final error
                                    # Let's add it directly to the step dict temporarily? Or a dedicated failure context?
                                    # For now, log it and include basics in the AssertionError
                                    run_status["visual_failure_details"] = visual_failure_details # Add to main run status
                                    raise AssertionError(failure_msg) # Fail the step

                            visual_result = {
                                "step_id": step_id,
                                "baseline_id": baseline_id,
                                "status": "PASS" if pixel_match_passed else "FAIL",
                                "pixel_difference_ratio": diff_ratio,
                                "mismatched_pixels": mismatched_pixels,
                                "pixel_threshold": step_threshold,
                                "llm_override": use_llm and not pixel_match_passed and llm_response.strip().upper().startswith("YES") if 'llm_response' in locals() else False,
                                "llm_reasoning": llm_reasoning,
                                "diff_image_path": diff_image_path,
                                "element_selector": element_selector
                            }
                            run_status["visual_assertion_results"].append(visual_result)
       

                        elif action == "assert_passed_verification" or action == "assert_llm_verification":
                            if not self.llm_client:
                                raise PlaywrightError("LLMClient not available for vision-based verification step.")
                            if not description:
                                raise ValueError("Missing 'description' field for 'assert_passed_verification' step.")
                            if not self.browser_controller:
                                raise PlaywrightError("BrowserController not available for state gathering.")

                            logger.info("Performing vision-based verification with DOM context...")

                            # --- Gather Context ---
                            screenshot_bytes = self.browser_controller.take_screenshot()
                            current_url = self.browser_controller.get_current_url()
                            dom_context_str = "DOM context could not be retrieved." # Default
                            try:
                                dom_state = self.browser_controller.get_structured_dom(highlight_all_clickable_elements=False, viewport_expansion=-1) # No highlight during execution verification
                                if dom_state and dom_state.element_tree:
                                    # Use 'verification' purpose for potentially richer context
                                    dom_context_str, _ = dom_state.element_tree.generate_llm_context_string(context_purpose='verification')
                                else:
                                    logger.warning("Failed to get valid DOM state for vision verification.")
                            except Exception as dom_err:
                                logger.error(f"Error getting DOM context for vision verification: {dom_err}", exc_info=True)
                            # --------------------

                            if not screenshot_bytes:
                                raise PlaywrightError("Failed to capture screenshot for vision verification.")


                            prompt = f"""Analyze the provided webpage screenshot AND the accompanying HTML context.

    The goal during testing was to verify the following condition: "{description}"
    Current URL: {current_url}

    HTML Context (Visible elements, interactive elements marked with `[index]`, static with `(Static)`):
    ```html
    {dom_context_str}
    ```

    Based on BOTH the visual evidence in the screenshot AND the HTML context (Prioritize html context more as screenshot will have some delay from when it was asked and when it was taken), is the verification condition "{description}" currently met?
    If you think due to the delay in html AND screenshot, state might have changed from where the condition was met, then also respond with YES

    IMPORTANT: Consider that elements might be in a loading state (e.g., placeholders described) OR a fully loaded state (e.g., actual images shown visually). If the current state reasonably fulfills the ultimate goal implied by the description (even if the exact visual differs due to loading, like placeholders becoming images), respond YES.

    Respond with only "YES" or "NO", followed by a brief explanation justifying your answer using evidence from the screenshot and/or HTML context.
    Example Response (Success): YES - The 'Welcome, User!' message [Static id='s15'] is visible in the HTML and visually present at the top of the screenshot.
    Example Response (Failure): NO - The HTML context shows an error message element [12] and the screenshot visually confirms the 'Invalid credentials' error.
    Example Response (Success - Placeholder Intent): YES - The description asked for 5 placeholders, but the screenshot and HTML show 5 fully loaded images within the expected containers ('div.image-container'). This fulfills the intent of ensuring the 5 image sections are present and populated.
    """


                            llm_response = self.llm_client.generate_multimodal(prompt, screenshot_bytes)
                            logger.debug(f"Vision verification LLM response: {llm_response}")

                            if llm_response.strip().upper().startswith("YES"):
                                logger.info("✅ Vision verification PASSED (with DOM context).")
                            elif llm_response.strip().upper().startswith("NO"):
                                logger.error(f"❌ Vision verification FAILED (with DOM context). LLM Reasoning: {llm_response}")
                                raise AssertionError(f"Vision verification failed: Condition '{description}' not met. LLM Reason: {llm_response}")
                            elif llm_response.startswith("Error:"):
                                logger.error(f"❌ Vision verification FAILED due to LLM error: {llm_response}")
                                raise PlaywrightError(f"Vision verification LLM error: {llm_response}")
                            else:
                                logger.error(f"❌ Vision verification FAILED due to unclear LLM response: {llm_response}")
                                raise AssertionError(f"Vision verification failed: Unclear LLM response. Response: {llm_response}")
                        # --- Add more actions/assertions as needed ---
                        else:
                            logger.warning(f"Unsupported action type '{action}' found in step {step_id}. Skipping.")
                            # Optionally treat as failure: raise ValueError(f"Unsupported action: {action}")

                        
                        step_healed = True
                        log_suffix = ""
                        if current_healing_attempts > 0:
                            # Store the selector that *worked* (which is current_selector)
                            successful_healed_selector_for_step = current_selector
                            log_suffix = f" (Healed after {current_healing_attempts} attempt(s) using selector '{current_selector}')"

                        logger.info(f"Step {step_id} completed successfully{log_suffix}.")

                        
                        logger.info(f"Step {step_id} completed successfully.")

                        # Optional wait after successful step execution
                        if wait_after > 0:
                            logger.debug(f"Waiting for {wait_after}s after step {step_id}...")
                            time.sleep(wait_after)
                        
                    except (PlaywrightError, PlaywrightTimeoutError, ValueError, AssertionError) as e:
                        # Catch Playwright errors, input errors, and assertion failures (from expect)
                        last_error = e # Store the error
                        error_type = type(e).__name__
                        error_msg = str(e)
                        logger.warning(f"Attempt {current_healing_attempts + 1} for Step {step_id} failed. Error: {error_type}: {error_msg}")
                        
                        # --- Healing Decision Logic ---
                        is_healable_error = isinstance(e, (PlaywrightTimeoutError, PlaywrightError)) and current_selector is not None
                        # Refine healable conditions:
                        # - Timeout finding/interacting with an element
                        # - Element detached, not visible, not interactable (if selector exists)
                        # - Exclude navigation errors, value errors from missing params, count mismatches
                        if isinstance(e, ValueError) or (isinstance(e, AssertionError) and "count" in error_msg.lower()):
                            is_healable_error = False
                        if action == "navigate":
                            is_healable_error = False
                        if action == "assert_visual_match":
                            is_healable_error = False

                        can_attempt_healing = self.enable_healing and is_healable_error and current_healing_attempts < self.healing_retries_per_step

                        if can_attempt_healing:
                            logger.info(f"Attempting Healing (Mode: {self.healing_mode}) for Step {step_id}...")
                            healing_success = False
                            new_selector = None
                            healing_log_entry = {
                                "step_id": step_id,
                                "attempt": current_healing_attempts + 1,
                                "mode": self.healing_mode,
                                "success": False,
                                "original_selector": original_selector,
                                "failed_selector": current_selector,
                                "error": f"{error_type}: {error_msg}",
                                "new_selector": None,
                                "reasoning": None,
                            }

                            if self.healing_mode == 'soft':
                                healing_success, new_selector, reasoning = self._attempt_soft_healing(step, current_selector, error_msg)
                                healing_log_entry["new_selector"] = new_selector
                                healing_log_entry["reasoning"] = reasoning
                                if healing_success:
                                    logger.info(f"Soft healing successful for Step {step_id}. New selector: '{new_selector}'")
                                    current_selector = new_selector # Update selector for the next loop iteration
                                    healing_log_entry["success"] = True
                                else:
                                    logger.warning(f"Soft healing failed for Step {step_id}. Reason: {reasoning}")
                                    # Let the loop proceed to final failure state below

                            elif self.healing_mode == 'hard':
                                logger.warning(f"Hard Healing triggered for Step {step_id} due to error: {error_msg}")
                                if self.browser_controller:
                                     self.browser_controller.clear_console_messages()
                                     self.browser_controller.clear_network_requests()
                                healing_log_entry["mode"] = "hard" # Log mode
                                healing_log_entry["success"] = True # Mark attempt as 'successful' in triggering re-record
                                self.healing_attempts_log.append(healing_log_entry) # Log before triggering
                                self._trigger_hard_healing(feature_description, json_file_path)
                                run_status["status"] = "HEALING_TRIGGERED"
                                run_status["message"] = f"Hard Healing (re-recording) triggered on Step {step_id}."
                                run_status["failed_step"] = step # Store the step that triggered it
                                run_status["error_details"] = f"Hard healing triggered by {error_type}: {error_msg}"
                                return run_status # Stop execution and return status

                            self.healing_attempts_log.append(healing_log_entry) # Log soft healing attempt

                            if healing_success:
                                current_healing_attempts += 1
                                continue # Go to the next iteration of the while loop to retry with new selector
                            else:
                                # Soft healing failed, break the while loop to handle final failure
                                current_healing_attempts = self.healing_retries_per_step + 1

                        else:
                             # Healing not enabled, max attempts reached, or not a healable error
                             logger.error(f"❌ Step {step_id} failed permanently. Healing skipped or failed.")
                             raise last_error # Re-raise the last error to trigger final failure handling


                # --- End Healing Loop ---

                if successful_healed_selector_for_step:
                    logger.info(f"Persisting healed selector for Step {step_id}: '{successful_healed_selector_for_step}'")
                    # Modify the step in the IN-MEMORY list 'steps'
                    if i < len(steps): # Check index boundary
                        steps[i]['selector'] = successful_healed_selector_for_step
                        any_step_successfully_healed = True
                        run_status["healed_steps_count"] += 1
                    else:
                         logger.error(f"Index {i} out of bounds for steps list while persisting healed selector for step {step_id}.")
                
                # If the while loop finished because max attempts were reached without success
                if not step_healed:
                    logger.error(f"❌ Step {step_id} ('{description}') Failed definitively after {current_healing_attempts} attempt(s).")
                    run_status["status"] = "FAIL"
                    run_status["message"] = f"Test failed on step {step_id}: {description}"
                    run_status["failed_step"] = step
                    # Use the last captured error
                    error_type = type(last_error).__name__ if last_error else "UnknownError"
                    error_msg = str(last_error) if last_error else "Step failed after healing attempts."
                    run_status["error_details"] = f"{error_type}: {error_msg}"
                    if run_status["status"] == "FAIL" and step.get("action") == "assert_visual_match" and "visual_failure_details" in run_status:
                        run_status["error_details"] += f"\nVisual Failure Details: {run_status['visual_failure_details']}"

                    # Failure Handling (Screenshot/Logs)
                    try:
                        ts = time.strftime("%Y%m%d_%H%M%S")
                        safe_test_name = re.sub(r'[^\w\-]+', '_', test_name)[:50]
                        screenshot_path = os.path.join("output", f"failure_{safe_test_name}_step{step_id}_{ts}.png")
                        if self.browser_controller and self.browser_controller.save_screenshot(screenshot_path):
                            run_status["screenshot_on_failure"] = screenshot_path
                            logger.info(f"Failure screenshot saved to: {screenshot_path}")
                        if self.browser_controller:
                            run_status["all_console_messages"] = self.browser_controller.get_console_messages()
                            run_status["console_messages_on_failure"] = [
                                msg for msg in run_status["all_console_messages"]
                                if msg['type'] in ['error', 'warning']
                            ][-5:]
                    except Exception as fail_handle_e:
                        logger.error(f"Error during failure handling: {fail_handle_e}")

                    # Stop the entire test execution
                    logger.info("Stopping test execution due to permanent step failure.")
                    return run_status # Return immediately
                
            # If loop completes without breaking due to permanent failure
            logger.info("--- Setting final status to PASS ---") 
            run_status["status"] = "PASS"
            run_status["message"] = "✅ Test executed successfully."
            if any_step_successfully_healed:
                run_status["message"] += f" ({run_status['healed_steps_count']} step(s) healed)."
            logger.info(run_status["message"])


        except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
            logger.error(f"Error loading or parsing test file '{json_file_path}': {e}")
            run_status["message"] = f"Failed to load/parse test file: {e}"
            run_status["error_details"] = str(e)
        except PlaywrightError as e: # Catch setup errors before loop
             logger.critical(f"Playwright setup error during execution: {e}", exc_info=True)
             run_status["message"] = f"Playwright setup failed: {e}"
             run_status["error_details"] = str(e)
        except Exception as e:
            # Catch errors from the execution loop if not already handled
            if run_status["status"] != "FAIL" and run_status["status"] != "HEALING_TRIGGERED":
                 logger.critical(f"An unexpected error occurred during execution: {e}", exc_info=True)
                 run_status["message"] = f"Unexpected execution error: {e}"
                 run_status["error_details"] = f"{type(e).__name__}: {e}"
                 run_status["status"] = "FAIL" # Ensure status is Fail
        finally:
            logger.info("--- Ending Test Execution ---")
            if self.browser_controller:
                if self.get_network_requests:
                    try: run_status["network_requests"] = self.browser_controller.get_network_requests()
                    except: logger.error("Failed to retrieve final network requests.")
                # Performance timing is captured after navigation, check if it exists
                if run_status.get("performance_timing") is None and self.get_performance is not False:
                    try: run_status["performance_timing"] = self.browser_controller.get_performance_timing()
                    except: logger.error("Failed to retrieve final performance timing.")
                # Console messages captured on failure or here
                if "all_console_messages" not in run_status or not run_status["all_console_messages"]:
                     try: run_status["all_console_messages"] = self.browser_controller.get_console_messages()
                     except: logger.error("Failed to retrieve final console messages.")

                self.browser_controller.close()
                self.browser_controller = None
                self.page = None
                
            end_time = time.time()
            run_status["duration_seconds"] = round(end_time - start_time, 2)
            run_status["healing_attempts"] = self.healing_attempts_log
            
            if any_step_successfully_healed and run_status["status"] != "HEALING_TRIGGERED" and run_status["status"] == "PASS": # Save if healing occurred and not hard-healing
                try:
                    logger.info(f"Saving updated test file with {run_status['healed_steps_count']} healed step(s) to: {json_file_path}")
                    # modified_test_data should contain the updated steps list
                    with open(json_file_path, 'w', encoding='utf-8') as f:
                         json.dump(modified_test_data, f, indent=2, ensure_ascii=False)
                    run_status["healed_file_saved"] = True
                    logger.info(f"Successfully saved healed test file: {json_file_path}")
                    # Adjust final message if test passed after healing
                    if run_status["status"] == "PASS":
                        run_status["message"] = f"✅ Test passed with {run_status['healed_steps_count']} step(s) healed. Updated test file saved."
                except Exception as save_err:
                     logger.error(f"Failed to save healed test file '{json_file_path}': {save_err}", exc_info=True)
                     run_status["healed_file_saved"] = False
                     # Add warning to message if save failed
                     if run_status["status"] == "PASS":
                          run_status["message"] += " (Warning: Failed to save healed selectors)"
            logger.info(f"Execution finished in {run_status['duration_seconds']:.2f} seconds. Status: {run_status['status']}")

        return run_status
    