# executor.py
import json
import logging
import time
import os
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError, expect
from typing import Optional, Dict, Any
import re
# Use relative import for BrowserController if needed, or assume it's available
from browser_controller import BrowserController # Re-use for browser setup/teardown

logger = logging.getLogger(__name__)

class TestExecutor:
    """
    Executes a recorded test case from a JSON file deterministically using Playwright.
    """

    def __init__(self, headless: bool = True, default_timeout: int = 15000): # Default timeout for actions/assertions
        self.headless = headless
        self.default_timeout = default_timeout # Milliseconds
        self.browser_controller: Optional[BrowserController] = None
        self.page: Optional[Page] = None
        logger.info(f"TestExecutor initialized (headless={headless}, timeout={default_timeout}ms).")

    def _get_locator(self, selector: str):
        """Helper to get a Playwright locator, handling potential errors."""
        if not self.page:
            raise PlaywrightError("Page is not initialized.")
        if not selector:
            raise ValueError("Selector cannot be empty.")
        try:
            return self.page.locator(selector).first
        except Exception as e:
            # Catch errors during locator creation itself (e.g., invalid selector syntax)
            logger.error(f"Failed to create locator for selector: '{selector}'. Error: {e}")
            raise PlaywrightError(f"Invalid selector syntax or error creating locator: '{selector}'. Error: {e}") from e

    def run_test(self, json_file_path: str) -> Dict[str, Any]:
        """Loads and executes the test steps from the JSON file."""
        start_time = time.time()
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
            "duration_seconds": 0.0,
        }

        try:
            # --- Load Test Data ---
            logger.info(f"Loading test case from: {json_file_path}")
            if not os.path.exists(json_file_path):
                 raise FileNotFoundError(f"Test file not found: {json_file_path}")
            with open(json_file_path, 'r', encoding='utf-8') as f:
                test_data = json.load(f)

            steps = test_data.get("steps", [])
            test_name = test_data.get("test_name", "Unnamed Test")
            run_status["test_name"] = test_name
            logger.info(f"Executing test: '{test_name}' with {len(steps)} steps.")

            if not steps:
                raise ValueError("No steps found in the test file.")

            # --- Setup Browser ---
            self.browser_controller = BrowserController(headless=self.headless)
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

            # --- Execute Steps ---
            for i, step in enumerate(steps):
                step_id = step.get("step_id", i + 1)
                action = step.get("action")
                selector = step.get("selector")
                params = step.get("parameters", {})
                description = step.get("description", f"Step {step_id}")
                wait_after = step.get("wait_after_secs", 0) # Get wait time

                run_status["steps_executed"] = i + 1 # Track steps attempted
                logger.info(f"--- Executing Step {step_id}: {action} - {description} ---")
                if selector: logger.info(f"Selector: {selector}")
                if params: logger.info(f"Parameters: {params}")

                try:
                    if action == "navigate":
                        url = params.get("url")
                        if not url: raise ValueError("Missing 'url' parameter for navigate.")
                        self.page.goto(url) # Uses default navigation timeout from context
                    elif action == "click":
                        if not selector: raise ValueError("Missing 'selector' for click.")
                        locator = self._get_locator(selector)
                        locator.click(timeout=self.default_timeout) # Explicit timeout for action
                    elif action == "type":
                        text = params.get("text")
                        if not selector: raise ValueError("Missing 'selector' for type.")
                        if text is None: raise ValueError("Missing 'text' parameter for type.")
                        locator = self._get_locator(selector)
                        locator.fill(text, timeout=self.default_timeout) # Use fill for robustness
                    elif action == "scroll": # Less common, but support if recorded
                         direction = params.get("direction")
                         if direction not in ["up", "down"]: raise ValueError("Invalid 'direction'.")
                         amount = "window.innerHeight" if direction=="down" else "-window.innerHeight"
                         self.page.evaluate(f"window.scrollBy(0, {amount})")
                    elif action == "check": # <-- New Action
                         if not selector: raise ValueError("Missing 'selector' for check action.")
                         # Use the browser_controller method which handles locator/timeout
                         self.browser_controller.check(selector)
                    elif action == "uncheck": # <-- New Action
                         if not selector: raise ValueError("Missing 'selector' for uncheck action.")
                         # Use the browser_controller method
                         self.browser_controller.uncheck(selector)
                    elif action == "wait_for_load_state":
                         state = params.get("state", "load")
                         self.page.wait_for_load_state(state, timeout=self.browser_controller.default_navigation_timeout) # Use navigation timeout
                    elif action == "wait_for_selector": # Explicit wait
                         wait_state = params.get("state", "visible")
                         timeout = params.get("timeout_ms", self.default_timeout)
                         if not selector: raise ValueError("Missing 'selector' for wait_for_selector.")
                         locator = self._get_locator(selector)
                         locator.wait_for(state=wait_state, timeout=timeout)
                    # --- Assertions ---
                    elif action == "assert_text_contains":
                        expected_text = params.get("expected_text")
                        if not selector: raise ValueError("Missing 'selector' for assertion.")
                        if expected_text is None: raise ValueError("Missing 'expected_text'.")
                        locator = self._get_locator(selector)
                        expect(locator).to_contain_text(expected_text, timeout=self.default_timeout)
                    elif action == "assert_text_equals":
                        expected_text = params.get("expected_text")
                        if not selector: raise ValueError("Missing 'selector' for assertion.")
                        if expected_text is None: raise ValueError("Missing 'expected_text'.")
                        locator = self._get_locator(selector)
                        expect(locator).to_have_text(expected_text, timeout=self.default_timeout)
                    elif action == "assert_visible":
                        if not selector: raise ValueError("Missing 'selector' for assertion.")
                        locator = self._get_locator(selector)
                        expect(locator).to_be_visible(timeout=self.default_timeout)
                    elif action == "assert_hidden":
                        if not selector: raise ValueError("Missing 'selector' for assertion.")
                        locator = self._get_locator(selector)
                        expect(locator).to_be_hidden(timeout=self.default_timeout)
                    elif action == "assert_attribute_equals":
                        attr_name = params.get("attribute_name")
                        expected_value = params.get("expected_value")
                        if not selector: raise ValueError("Missing 'selector' for assertion.")
                        if not attr_name: raise ValueError("Missing 'attribute_name'.")
                        if expected_value is None: raise ValueError("Missing 'expected_value'.")
                        locator = self._get_locator(selector)
                        expect(locator).to_have_attribute(attr_name, expected_value, timeout=self.default_timeout)
                    elif action == "assert_element_count":
                         expected_count = params.get("expected_count")
                         if not selector: raise ValueError("Missing 'selector' for assertion.")
                         if expected_count is None: raise ValueError("Missing 'expected_count'.")
                         locator = self._get_locator(selector)
                         expect(locator).to_have_count(expected_count, timeout=self.default_timeout)
                    elif action == "assert_checked":
                         if not selector: raise ValueError("Missing 'selector' for assert_checked.")
                         locator = self._get_locator(selector)
                         # Use Playwright's dedicated assertion for checked state
                         expect(locator).to_be_checked(timeout=self.default_timeout)
                    elif action == "assert_not_checked":
                         if not selector: raise ValueError("Missing 'selector' for assert_not_checked.")
                         locator = self._get_locator(selector)
                         # Use .not modifier with the checked assertion
                         expect(locator).not_to_be_checked(timeout=self.default_timeout)
                    # --- Add more actions/assertions as needed ---
                    else:
                        logger.warning(f"Unsupported action type '{action}' found in step {step_id}. Skipping.")
                        # Optionally treat as failure: raise ValueError(f"Unsupported action: {action}")

                    # Optional wait after successful step execution
                    if wait_after > 0:
                         logger.debug(f"Waiting for {wait_after}s after step {step_id}...")
                         time.sleep(wait_after)

                    logger.info(f"Step {step_id} completed successfully.")

                except (PlaywrightError, PlaywrightTimeoutError, ValueError, AssertionError) as e:
                    # Catch Playwright errors, input errors, and assertion failures (from expect)
                    error_type = type(e).__name__
                    error_msg = str(e)
                    logger.error(f"❌ Step {step_id} ('{description}') Failed! Type: {error_type}")
                    logger.error(f"   Error: {error_msg}")
                    run_status["status"] = "FAIL"
                    run_status["message"] = f"Test failed on step {step_id}: {description}"
                    run_status["failed_step"] = step # Store failed step info
                    run_status["error_details"] = f"{error_type}: {error_msg}"

                    # --- Failure Handling ---
                    try:
                        # Save screenshot
                        ts = time.strftime("%Y%m%d_%H%M%S")
                        safe_test_name = re.sub(r'[^\w\-]+', '_', test_name)[:50]
                        screenshot_path = os.path.join("output", f"failure_{safe_test_name}_step{step_id}_{ts}.png")
                        if self.browser_controller.save_screenshot(screenshot_path):
                            run_status["screenshot_on_failure"] = screenshot_path
                            logger.info(f"Failure screenshot saved to: {screenshot_path}")
                        else:
                            logger.warning("Failed to save screenshot on failure.")

                        # Get console logs
                        run_status["all_console_messages"] = self.browser_controller.get_console_messages()
                        run_status["console_messages_on_failure"] = [
                            msg for msg in run_status["all_console_messages"]
                            if msg['type'] in ['error', 'warning']
                        ][-5:] # Last 5 errors/warnings

                    except Exception as fail_handle_e:
                        logger.error(f"Error during failure handling (screenshot/logs): {fail_handle_e}")
                    # --- End Failure Handling ---

                    raise e # Re-raise the exception to stop execution

            # If loop completes without errors
            run_status["status"] = "PASS"
            run_status["message"] = "✅ Test executed successfully."
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
            # Catch errors from the execution loop (already logged) or unexpected errors
            if run_status["status"] != "FAIL": # If it wasn't already set by step failure
                 logger.critical(f"An unexpected error occurred during execution: {e}", exc_info=True)
                 run_status["message"] = f"Unexpected execution error: {e}"
                 run_status["error_details"] = f"{type(e).__name__}: {e}"
            # Keep the status as FAIL if it was already set

        finally:
            logger.info("--- Ending Test Execution ---")
            if self.browser_controller:
                # Capture final logs if not already done on failure
                if "all_console_messages" not in run_status or not run_status["all_console_messages"]:
                     try: run_status["all_console_messages"] = self.browser_controller.get_console_messages()
                     except: pass # Ignore errors during final log capture

                self.browser_controller.close()
                self.browser_controller = None
                self.page = None

            end_time = time.time()
            run_status["duration_seconds"] = round(end_time - start_time, 2)
            logger.info(f"Execution finished in {run_status['duration_seconds']:.2f} seconds. Status: {run_status['status']}")

        return run_status