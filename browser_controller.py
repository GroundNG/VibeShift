# browser_controller.py
from playwright.sync_api import sync_playwright, Page, Browser, Playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError, Locator, ConsoleMessage
import logging
import time
import random
import json
import os
from typing import Optional, Any, Dict, List

from dom.service import DomService
from dom.views import DOMState, DOMElementNode, SelectorMap

logger = logging.getLogger(__name__)

COMMON_HEADERS = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br',
    'Accept-Language': 'en-US,en;q=0.9',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Upgrade-Insecure-Requests': '1',
}

HIDE_WEBDRIVER_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {
  get: () => undefined
});
"""


class BrowserController:
    """Handles Playwright browser automation tasks, including console message capture."""

    def __init__(self, headless=True):
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: Optional[Any] = None # Keep context reference
        self.page: Page | None = None
        self.headless = headless
        self.default_navigation_timeout = 90000
        self.default_action_timeout = 20000
        self._dom_service: Optional[DomService] = None
        self.console_messages: List[Dict[str, Any]] = [] # <-- Add list to store messages
        logger.info(f"BrowserController initialized (headless={headless}).")

    def _handle_console_message(self, message: ConsoleMessage):
        """Callback function to handle console messages."""
        msg_type = message.type
        msg_text = message.text
        timestamp = time.time()
        log_entry = {
            "timestamp": timestamp,
            "type": msg_type,
            "text": msg_text,
            # Optional: Add location if needed, but can be verbose
            # "location": message.location()
        }
        self.console_messages.append(log_entry)
        # Optional: Log immediately to agent's log file for real-time debugging
        log_level = logging.WARNING if msg_type in ['error', 'warning'] else logging.DEBUG
        logger.log(log_level, f"[CONSOLE.{msg_type.upper()}] {msg_text}")


    def start(self):
        """Starts Playwright, launches browser, creates context/page, and attaches console listener."""
        try:
            logger.info("Starting Playwright...")
            self.playwright = sync_playwright().start()
            # Consider adding args for anti-detection if needed:
            browser_args = ['--disable-blink-features=AutomationControlled']
            self.browser = self.playwright.chromium.launch(headless=self.headless, args=browser_args)
            # self.browser = self.playwright.chromium.launch(headless=self.headless)

            self.context = self.browser.new_context(
                 user_agent=self._get_random_user_agent(),
                 viewport=self._get_random_viewport(),
                 ignore_https_errors=True,
                 java_script_enabled=True,
                 extra_http_headers=COMMON_HEADERS,
            )
            self.context.set_default_navigation_timeout(self.default_navigation_timeout)
            self.context.set_default_timeout(self.default_action_timeout)
            self.context.add_init_script(HIDE_WEBDRIVER_SCRIPT)

            self.page = self.context.new_page()

            # Initialize DomService with the created page
            self._dom_service = DomService(self.page) # Instantiate here
            
            # --- Attach Console Listener ---
            self.page.on('console', self._handle_console_message)
            logger.info("Attached console message listener to the page.")
            # -----------------------------

            logger.info("Browser context and page created.")
            logger.info(f"Using User-Agent: {self.page.evaluate('navigator.userAgent')}")

        except Exception as e:
            logger.error(f"Failed to start Playwright or launch browser: {e}", exc_info=True)
            self.close() # Ensure cleanup on failure
            raise
    
    def get_console_messages(self) -> List[Dict[str, Any]]:
        """Returns a copy of the captured console messages."""
        return list(self.console_messages) # Return a copy

    def clear_console_messages(self):
        """Clears the stored console messages."""
        logger.debug("Clearing captured console messages.")
        self.console_messages = []


    def _get_random_user_agent(self):
        """Provides a random choice from a list of common user agents."""
        user_agents = [
            # Chrome on Windows
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36',
             # Chrome on Mac
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
            # Firefox on Windows
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0',
            # Add more variations if desired (Edge, Safari etc.)
        ]
        return random.choice(user_agents)

    def _get_random_viewport(self):
        """Provides a slightly randomized common viewport size."""
        common_sizes = [
            {'width': 1920, 'height': 1080},
            {'width': 1366, 'height': 768},
            {'width': 1440, 'height': 900},
            {'width': 1536, 'height': 864},
        ]
        base = random.choice(common_sizes)
        # Add small random offset
        base['width'] += random.randint(-10, 10)
        base['height'] += random.randint(-5, 5)
        return base

    def close(self):
        """Closes the browser and stops Playwright."""
        try:
            self._dom_service = None
            if self.context:
                 self.context.close()
                 logger.info("Browser context closed.")
            if self.browser:
                self.browser.close()
                logger.info("Browser closed.")
            if self.playwright:
                self.playwright.stop()
                logger.info("Playwright stopped.")
        except Exception as e:
            logger.error(f"Error during browser/Playwright cleanup: {e}", exc_info=True)
        finally:
            self.page = None
            self.context = None
            self.browser = None
            self.playwright = None
            self.console_messages = [] # Clear messages on final close

    def get_structured_dom(self, highlight_elements: bool = True, viewport_expansion: int = 0) -> Optional[DOMState]:
        """
        Uses DomService to get a structured representation of the interactive DOM elements.

        Args:
            highlight_elements: Whether to visually highlight elements in the browser.
            viewport_expansion: Pixel value to expand the viewport for element detection (0=viewport only, -1=all).

        Returns:
            A DOMState object containing the element tree and selector map, or None on error.
        """
        if not self.page or not self._dom_service:
            logger.error("Browser/Page not initialized or DomService unavailable.")
            return None
        try:
            logger.info(f"Requesting structured DOM (highlight={highlight_elements}, expansion={viewport_expansion})...")
            start_time = time.time()
            dom_state = self._dom_service.get_clickable_elements(
                highlight_elements=highlight_elements,
                focus_element=-1, # Not focusing on a specific element for now
                viewport_expansion=viewport_expansion
            )
            end_time = time.time()
            logger.info(f"Structured DOM retrieved in {end_time - start_time:.2f}s. Found {len(dom_state.selector_map)} interactive elements.")
            # Optional: Generate CSS selectors immediately after getting the state
            # for node in dom_state.selector_map.values():
            #      if not node.css_selector:
            #           node.css_selector = DomService._enhanced_css_selector_for_element(node)

            return dom_state
        except Exception as e:
            logger.error(f"Error getting structured DOM: {type(e).__name__}: {e}", exc_info=True)
            return None
    
    def get_selector_for_node(self, node: DOMElementNode) -> Optional[str]:
        """Generates a robust CSS selector for a given DOMElementNode."""
        if not node: return None
        try:
            # Use the static method from DomService
            return DomService._enhanced_css_selector_for_element(node)
        except Exception as e:
             logger.error(f"Error generating selector for node {node.xpath}: {e}", exc_info=True)
             return node.xpath # Fallback to xpath
    
    def goto(self, url: str):
        """Navigates the page to a specific URL."""
        if not self.page:
            raise PlaywrightError("Browser not started. Call start() first.")
        try:
            logger.info(f"Navigating to URL: {url}")
            # Use default navigation timeout set in context
            response = self.page.goto(url, wait_until='domcontentloaded', timeout=self.default_navigation_timeout) # 'load' or 'networkidle' might be better sometimes
            # Add a small stable delay after load
            time.sleep(2)
            status = response.status if response else 'unknown'
            logger.info(f"Navigation to {url} finished with status: {status}.")
            if response and not response.ok:
                 logger.warning(f"Navigation to {url} resulted in non-OK status: {status}")
                 # Optionally raise an error here if needed
        except PlaywrightTimeoutError as e:
            logger.error(f"Timeout navigating to {url}: {e}")
            # Re-raise with a clearer message for the agent
            raise PlaywrightTimeoutError(f"Timeout loading page {url}. The page might be too slow or unresponsive.") from e
        except PlaywrightError as e: # Catch broader Playwright errors
            logger.error(f"Playwright error navigating to {url}: {e}")
            raise PlaywrightError(f"Error navigating to {url}: {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error navigating to {url}: {e}", exc_info=True)
            raise # Re-raise for the agent to handle

    def get_html(self) -> str:
        """Returns the full HTML content of the current page."""
        if not self.page:
            logger.error("Cannot get HTML, browser not started.")
            raise Exception("Browser not started.")
        try:
            html = self.page.content()
            logger.info("Retrieved page HTML content.")
            # logger.debug(f"HTML content length: {len(html)}")
            return html
        except Exception as e:
            logger.error(f"Error getting HTML content: {e}", exc_info=True)
            return f"Error retrieving HTML: {e}"

    def get_current_url(self) -> str:
        """Returns the current URL of the page."""
        if not self.page:
            return "Error: Browser not started."
        try:
            return self.page.url
        except Exception as e:
            logger.error(f"Error getting current URL: {e}", exc_info=True)
            return f"Error retrieving URL: {e}"


    def take_screenshot(self) -> bytes | None:
        """Takes a screenshot of the current page and returns bytes."""
        if not self.page:
            logger.error("Cannot take screenshot, browser not started.")
            return None
        try:
            screenshot_bytes = self.page.screenshot()
            logger.info("Screenshot taken (bytes).")
            return screenshot_bytes
        except Exception as e:
            logger.error(f"Error taking screenshot: {e}", exc_info=True)
            return None
        
    def save_screenshot(self, file_path: str) -> bool:
        """Takes a screenshot and saves it to the specified file path."""
        if not self.page:
            logger.error(f"Cannot save screenshot to {file_path}, browser not started.")
            return False
        try:
            # Ensure directory exists
            abs_file_path = os.path.abspath(file_path)
            os.makedirs(os.path.dirname(abs_file_path), exist_ok=True)

            self.page.screenshot(path=abs_file_path)
            logger.info(f"Screenshot saved to: {abs_file_path}")
            return True
        except Exception as e:
            logger.error(f"Error saving screenshot to {file_path}: {e}", exc_info=True)
            return False

    def _find_element(self, selector: str, timeout=None) -> Optional[Locator]:
        """Finds the first element matching the selector."""
        if not self.page:
             raise PlaywrightError("Browser not started.")
        effective_timeout = timeout if timeout is not None else self.default_action_timeout
        logger.debug(f"Attempting to find element: '{selector}' (timeout: {effective_timeout}ms)")
        try:
             # Use locator().first to explicitly target the first match
             element = self.page.locator(selector).first
             # Brief wait for attached state, primary checks in actions
             element.wait_for(state='attached', timeout=effective_timeout * 0.5)
             # Scroll into view if needed
             try:
                  element.scroll_into_view_if_needed(timeout=effective_timeout * 0.25)
                  time.sleep(0.1)
             except Exception as scroll_e:
                  logger.warning(f"Non-critical: Could not scroll element {selector} into view. Error: {scroll_e}")
             logger.debug(f"Element found and attached: '{selector}'")
             return element
        except PlaywrightTimeoutError:
             # Don't log as error here, actions will report failure if needed
             logger.debug(f"Timeout ({effective_timeout}ms) waiting for element state 'attached' or scrolling: '{selector}'.")
             return None
        except PlaywrightError as e:
            logger.error(f"PlaywrightError finding element '{selector}': {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error finding element '{selector}': {e}", exc_info=True)
            return None
    
    def click(self, selector: str):
        """Clicks an element, relying on Playwright's built-in actionability checks."""
        if not self.page:
            raise PlaywrightError("Browser not started.")
        try:
            logger.info(f"Attempting to click element: {selector}")
            locator = self.page.locator(selector).first #
            logger.debug(f"Executing click on locator for '{selector}' (with built-in checks)...")
            click_delay = random.uniform(50, 150)

            # Optional: Try hover first
            try:
                locator.hover(timeout=3000) # Short timeout for hover
                self._human_like_delay(0.1, 0.3)
            except Exception:
                logger.debug(f"Hover failed or timed out for {selector}, proceeding with click.")

            # Perform the click with its own timeout
            locator.click(delay=click_delay, timeout=self.default_action_timeout)
            logger.info(f"Clicked element: {selector}")
            self._human_like_delay(0.5, 1.5) # Post-click delay

        except PlaywrightTimeoutError as e:
            # Timeout occurred *during* the click action's internal waits
            logger.error(f"Timeout ({self.default_action_timeout}ms) waiting for element '{selector}' to be actionable for click. Element might be obscured, disabled, unstable, or not found.")
            # Add more context to the error message
            screenshot_path = f"output/click_timeout_{selector.replace(' ','_').replace(':','_').replace('>','_')[:30]}_{int(time.time())}.png"
            self.save_screenshot(screenshot_path)
            logger.error(f"Saved screenshot on click timeout to: {screenshot_path}")
            raise PlaywrightTimeoutError(f"Timeout trying to click element: '{selector}'. Check visibility, interactability, and selector correctness. Screenshot saved to {screenshot_path}") from e
        except PlaywrightError as e:
             # Other errors during click
             logger.error(f"PlaywrightError clicking element '{selector}': {e}")
             raise PlaywrightError(f"Failed to click element '{selector}': {e}") from e
        except Exception as e:
             logger.error(f"Unexpected error clicking '{selector}': {e}", exc_info=True)
             raise PlaywrightError(f"Unexpected error clicking element '{selector}': {e}") from e

    def type(self, selector: str, text: str):
        """
        Inputs text into an element, prioritizing the robust `fill` method.
        Includes fallback to `type`.
        """
        if not self.page:
            raise PlaywrightError("Browser not started.")
        try:
            logger.info(f"Attempting to input text '{text[:30]}...' into element: {selector}")
            locator = self.page.locator(selector).first

            # --- Strategy 1: Use fill() ---
            # fill() clears the field first and inputs text.
            # It performs actionability checks (visible, enabled, editable etc.)
            
            logger.debug(f"Trying to 'fill' locator for '{selector}' (includes actionability checks)...")
            try:
                locator.fill(text, timeout=self.default_action_timeout) # Use default action timeout
                logger.info(f"'fill' successful for element: {selector}")
                self._human_like_delay(0.3, 0.8) # Delay after successful input
                return # Success! Exit the method.
            except (PlaywrightTimeoutError, PlaywrightError) as fill_error:
                logger.warning(f"'fill' action failed for '{selector}': {fill_error}. Attempting fallback to 'type'.")
            
            # Proceed to fallback

            # --- Strategy 2: Fallback to type() ---
            logger.debug(f"Trying fallback 'type' for locator '{selector}'...")
            try:
                # Ensure element is clear before typing as a fallback precaution
                locator.clear(timeout=self.default_action_timeout * 0.5) # Quick clear attempt
                self._human_like_delay(0.1, 0.3)
                typing_delay_ms = random.uniform(70, 150)
                locator.type(text, delay=typing_delay_ms, timeout=self.default_action_timeout)
                logger.info(f"Fallback 'type' successful for element: {selector}")
                self._human_like_delay(0.3, 0.8)
                return # Success!
            except (PlaywrightTimeoutError, PlaywrightError) as type_error:
                 logger.error(f"Both 'fill' and fallback 'type' failed for '{selector}'. Last error ('type'): {type_error}")
                 # Raise the error from the 'type' attempt as it was the last one tried
                 screenshot_path = f"output/type_fail_{selector.replace(' ','_').replace(':','_').replace('>','_')[:30]}_{int(time.time())}.png"
                 self.save_screenshot(screenshot_path)
                 logger.error(f"Saved screenshot on type failure to: {screenshot_path}")
                 # Raise a combined error or the last one
                 raise PlaywrightError(f"Failed to input text into element '{selector}' using both fill and type. Last error: {type_error}. Screenshot: {screenshot_path}") from type_error

        # Catch errors related to finding/interacting
        except PlaywrightTimeoutError as e:
             # This might catch timeouts from clear() or the actionability checks within fill/type
             logger.error(f"Timeout ({self.default_action_timeout}ms) during input operation stages for selector: '{selector}'. Element might not become actionable.")
             screenshot_path = f"output/input_timeout_{selector.replace(' ','_').replace(':','_').replace('>','_')[:30]}_{int(time.time())}.png"
             self.save_screenshot(screenshot_path)
             logger.error(f"Saved screenshot on input timeout to: {screenshot_path}")
             raise PlaywrightTimeoutError(f"Timeout trying to input text into element: '{selector}'. Check interactability. Screenshot: {screenshot_path}") from e
        except PlaywrightError as e:
             # Covers other Playwright issues like element detached during operation
             logger.error(f"PlaywrightError inputting text into element '{selector}': {e}")
             raise PlaywrightError(f"Failed to input text into element '{selector}': {e}") from e
        except Exception as e:
             logger.error(f"Unexpected error inputting text into '{selector}': {e}", exc_info=True)
             raise PlaywrightError(f"Unexpected error inputting text into element '{selector}': {e}") from e
             
    def scroll(self, direction: str):
        """Scrolls the page up or down with a slight delay."""
        if not self.page:
            raise PlaywrightError("Browser not started.")
        try:
            scroll_amount = "window.innerHeight"
            if direction == "down":
                self.page.evaluate(f"window.scrollBy(0, {scroll_amount})")
                logger.info("Scrolled down.")
            elif direction == "up":
                self.page.evaluate(f"window.scrollBy(0, -{scroll_amount})")
                logger.info("Scrolled up.")
            else:
                logger.warning(f"Invalid scroll direction: {direction}")
                return # Don't delay for invalid direction
            self._human_like_delay(0.4, 0.8) # Delay after scrolling
        except Exception as e:
            logger.error(f"Error scrolling {direction}: {e}", exc_info=True)
            
    def extract_text(self, selector: str) -> str:
        """Extracts the text content from the first element matching the selector."""
        if not self.page:
            raise PlaywrightError("Browser not started, cannot extract text.")
        try:
            logger.info(f"Extracting text from selector: {selector}")
            locator = self.page.locator(selector).first # Get locator

            # Use text_content() which has implicit waits, but add explicit short wait for visibility first
            try:
                 # Wait for element to be at least visible, maybe attached is enough?
                 # Let's stick to visible for text extraction. Use a shorter timeout.
                 locator.wait_for(state='visible', timeout=self.default_action_timeout * 0.75)
            except PlaywrightTimeoutError:
                 # Element didn't become visible in time
                 # Check if it exists but is hidden
                 is_attached = False
                 try:
                      is_attached = locator.is_attached() # Check if it's in DOM but hidden
                 except: pass # Ignore errors here

                 if is_attached:
                      logger.warning(f"Element '{selector}' found in DOM but is not visible within timeout for text extraction.")
                      return "Error: Element found but not visible"
                 else:
                      error_msg = f"Timeout waiting for element '{selector}' to be visible for text extraction."
                      logger.error(error_msg)
                      return f"Error: {error_msg}"

            # If visible, proceed to get text
            text = locator.text_content() # Get text content
            if text is None: text = ""
            logger.info(f"Successfully extracted text from '{selector}': '{text[:100]}...'")
            return text.strip()

        except PlaywrightError as e: # Catch other Playwright errors during text_content or wait_for
            logger.error(f"PlaywrightError extracting text from '{selector}': {e}")
            return f"Error extracting text: {type(e).__name__}: {e}"
        except Exception as e:
            logger.error(f"Unexpected error extracting text from '{selector}': {e}", exc_info=True)
            return f"Error extracting text: {type(e).__name__}: {e}"


    def extract_attributes(self, selector: str, attributes: List[str]) -> Dict[str, Optional[str]]:
        """Extracts specified attributes from the first element matching the selector."""
        if not self.page:
            raise PlaywrightError("Browser not started.")
        if not attributes:
             logger.warning("extract_attributes called with empty attributes list.")
             return {"error": "No attributes specified for extraction."} # Return error

        result_dict = {}
        try:
            logger.info(f"Extracting attributes {attributes} from selector: {selector}")
            locator = self.page.locator(selector).first # Get locator

            # Wait briefly for the element to be attached (don't need visibility necessarily for attributes)
            try:
                locator.wait_for(state='attached', timeout=self.default_action_timeout * 0.5)
            except PlaywrightTimeoutError:
                 error_msg = f"Timeout waiting for element '{selector}' to be attached for attribute extraction."
                 logger.error(error_msg)
                 return {"error": error_msg}

            # Element is attached, proceed
            for attr_name in attributes:
                try:
                     # get_attribute doesn't wait, element must exist
                     attr_value = locator.get_attribute(attr_name)
                     result_dict[attr_name] = attr_value
                     logger.debug(f"Extracted attribute '{attr_name}': '{str(attr_value)[:100]}...' from '{selector}'")
                except Exception as attr_e:
                     logger.warning(f"Could not extract attribute '{attr_name}' from '{selector}': {attr_e}")
                     result_dict[attr_name] = f"Error extracting: {attr_e}"

            logger.info(f"Finished extracting attributes {list(result_dict.keys())} from '{selector}'.")
            return result_dict

        except PlaywrightError as e: # Catch errors from wait_for or get_attribute
            logger.error(f"PlaywrightError extracting attributes {attributes} from '{selector}': {e}")
            return {"error": f"PlaywrightError extracting attributes from {selector}: {e}"}
        except Exception as e:
            logger.error(f"Unexpected error extracting attributes {attributes} from '{selector}': {e}", exc_info=True)
            return {"error": f"General error extracting attributes from {selector}: {e}"}


    def save_json_data(self, data: Any, file_path: str) -> dict:
        """
        Saves structured data as a JSON file to the given location.

        Args:
            data: The data to save (typically a dict or list that is JSON serializable).
            file_path: The path/filename where to save the JSON file (e.g., 'output/results.json').

        Returns:
            dict: Status of the operation with success flag, message, and file path.
        """
        try:
            # Ensure the directory exists
            abs_file_path = os.path.abspath(file_path)
            os.makedirs(os.path.dirname(abs_file_path), exist_ok=True)

            # Save the JSON data with pretty formatting
            with open(abs_file_path, 'a', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            logger.info(f"Successfully saved JSON data to {abs_file_path}")
            return {
                "success": True,
                "message": f"Data successfully saved to {abs_file_path}",
                "file_path": abs_file_path
            }
        except TypeError as e:
             logger.error(f"Data provided is not JSON serializable for file {file_path}: {e}", exc_info=True)
             return {
                 "success": False,
                 "message": f"Error saving JSON: Provided data is not serializable ({e})",
                 "error": str(e)
             }
        except Exception as e:
            logger.error(f"Error saving JSON data to {file_path}: {e}", exc_info=True)
            return {
                "success": False,
                "message": f"Error saving JSON data: {e}",
                "error": str(e)
            }

    def _human_like_delay(self, min_secs: float, max_secs: float):
        """ Sleeps for a random duration within the specified range. """
        delay = random.uniform(min_secs, max_secs)
        logger.debug(f"Applying human-like delay: {delay:.2f} seconds")
        time.sleep(delay)