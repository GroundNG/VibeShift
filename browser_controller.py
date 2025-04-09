# browser_controller.py
from playwright.sync_api import sync_playwright, Page, Browser, Playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError, Locator
import logging
import time
import random
import json  
import os  
from typing import Optional, Any, Dict, List

logger = logging.getLogger(__name__)

COMMON_HEADERS = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br', # Note: Playwright handles decoding
    'Accept-Language': 'en-US,en;q=0.9', # Common language
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none', # Varies, 'none' for initial nav
    'Sec-Fetch-User': '?1',
    'Upgrade-Insecure-Requests': '1',
    # User-Agent is set separately in new_context
    # Avoid setting Sec-CH-UA headers manually unless perfectly matching user agent
}

# JavaScript to attempt hiding webdriver flag
HIDE_WEBDRIVER_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {
  get: () => undefined
});
"""


class BrowserController:
    """Handles Playwright browser automation tasks."""

    def __init__(self, headless=True):
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.page: Page | None = None
        self.headless = headless
        self.default_navigation_timeout = 90000 # 90 seconds for page loads
        self.default_action_timeout = 20000 # 20 seconds for actions like click/type/find
        logger.info(f"BrowserController initialized (headless={headless}).")

    def start(self):
        """Starts Playwright, launches browser with anti-detection settings."""
        try:
            logger.info("Starting Playwright...")
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(headless=self.headless)

            # Create context with anti-detection measures
            self.context = self.browser.new_context(
                 user_agent=self._get_random_user_agent(), # Rotate User-Agent
                 viewport=self._get_random_viewport(), # Slightly randomize viewport
                 # locale='en-US', # Can also set locale
                 # timezone_id='America/New_York', # Can set timezone
                 ignore_https_errors=True, # Often needed for automation
                 java_script_enabled=True, # Ensure JS is enabled
                 extra_http_headers=COMMON_HEADERS,
            )
            self.context.set_default_navigation_timeout(self.default_navigation_timeout)
            self.context.set_default_timeout(self.default_action_timeout)

            # Add script to hide navigator.webdriver before page loads
            self.context.add_init_script(HIDE_WEBDRIVER_SCRIPT)

            self.page = self.context.new_page()
            logger.info("Browser context created with anti-detection measures.")
            logger.info(f"Using User-Agent: {self.context.pages[0].evaluate('navigator.userAgent')}") # Log the actual UA used

        except Exception as e:
            logger.error(f"Failed to start Playwright or launch browser: {e}", exc_info=True)
            if self.browser: self.browser.close() # Attempt cleanup
            if self.playwright: self.playwright.stop()
            raise

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

    def goto(self, url: str):
        """Navigates the page to a specific URL."""
        if not self.page:
            raise PlaywrightError("Browser not started. Call start() first.")
        try:
            logger.info(f"Navigating to URL: {url}")
            # Use default navigation timeout set in context
            response = self.page.goto(url, wait_until='domcontentloaded') # 'load' or 'networkidle' might be better sometimes
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
        """Takes a screenshot of the current page."""
        if not self.page:
            logger.error("Cannot take screenshot, browser not started.")
            return None
        try:
            screenshot_bytes = self.page.screenshot()
            logger.info("Screenshot taken.")
            return screenshot_bytes
        except Exception as e:
            logger.error(f"Error taking screenshot: {e}", exc_info=True)
            return None

    def _find_element(self, selector: str, timeout=None) -> Optional[Locator]:
        """
        Simplified helper to find an element, relying more on Playwright's
        built-in checks within actions like click() and fill().
        Waits primarily for the element to be attached and potentially visible.
        """
        if not self.page:
             raise PlaywrightError("Browser not started.")
        effective_timeout = timeout if timeout is not None else self.default_action_timeout
        logger.debug(f"Attempting to find element: '{selector}' (timeout: {effective_timeout}ms)")
        try:
             element = self.page.locator(selector).first
             # Wait for element to be attached to the DOM. Visibility/enabled checks
             # will be handled by the action itself (click, fill).
             logger.debug(f"Waiting for element '{selector}' to be attached...")
             # Use a shorter timeout here, actionability checks handle the rest
             element.wait_for(state='attached', timeout=effective_timeout * 0.5)

             # Optional: Scroll into view might still be helpful sometimes
             try:
                  logger.debug(f"Scrolling element '{selector}' into view if needed...")
                  element.scroll_into_view_if_needed(timeout=effective_timeout * 0.25)
                  time.sleep(0.1) # Small delay after potential scroll
             except Exception as scroll_e:
                  logger.warning(f"Could not scroll element {selector} into view, proceeding anyway. Error: {scroll_e}")


             logger.info(f"Element found and attached: '{selector}'")
             return element
        except PlaywrightTimeoutError as e:
             # Error during 'attached' wait or scroll
             logger.warning(f"Timeout ({effective_timeout}ms) waiting for element state 'attached' or scrolling: '{selector}'. Error: {e}")
             return None
        except PlaywrightError as e:
            logger.error(f"PlaywrightError finding element '{selector}': {e}")
            return None
        except Exception as e:
             logger.error(f"Unexpected error finding element '{selector}': {e}", exc_info=True)
             return None

    def click(self, selector: str):
        """Clicks an element, relying on Playwright's actionability checks."""
        if not self.page:
            raise PlaywrightError("Browser not started.")
        element = None
        try:
            logger.info(f"Attempting to click element: {selector}")
            # Find the element first (waits for attached state)
            element = self._find_element(selector)
            if element:
                 # Playwright's click() automatically waits for:
                 # - Element to be Visible
                 # - Element to be Stable (no animations)
                 # - Element to be Enabled
                 # - Element to receive events
                 logger.debug(f"Executing click on {selector} (with built-in actionability checks)...")
                 click_delay = random.uniform(50, 150) # Human-like pause during click

                 # Optional: Try hover first, but don't fail if it errors
                 try:
                     element.hover(timeout=3000)
                     self._human_like_delay(0.1, 0.3)
                 except Exception:
                     logger.debug(f"Hover failed for {selector}, continuing with click.")

                 element.click(delay=click_delay, timeout=self.default_action_timeout * 0.75) # Generous timeout for the click action itself
                 logger.info(f"Clicked element: {selector}")
                 self._human_like_delay(1.0, 2.0) # Post-click delay
            else:
                # _find_element failed (timeout finding/attaching)
                raise PlaywrightError(f"Element not found or not attached within timeout: {selector}")

        except PlaywrightTimeoutError as e:
            # Timeout occurred *during* the click action's internal waits
            logger.error(f"Timeout during click action's internal waits for selector: {selector}. Element might be obscured, disabled, or unstable.")
            raise PlaywrightTimeoutError(f"Timeout trying to click element: {selector}. Check visibility and interactability.") from e
        except PlaywrightError as e:
             # Other errors during click (e.g., detached during click)
             logger.error(f"PlaywrightError clicking element {selector}: {e}")
             raise PlaywrightError(f"Failed to click element {selector}: {e}") from e
        except Exception as e:
             logger.error(f"Unexpected error clicking {selector}: {e}", exc_info=True)
             raise PlaywrightError(f"Unexpected error clicking element {selector}: {e}") from e

    def type(self, selector: str, text: str):
        """
        Types text into an element, prioritizing the more robust `fill` method.
        Includes fallback to `type` if `fill` fails unexpectedly.
        """
        if not self.page:
            raise PlaywrightError("Browser not started.")
        element = None
        try:
            logger.info(f"Attempting to input text '{text[:20]}...' into element: {selector}")
            element = self._find_element(selector) # Waits for attached

            if element:
                # --- Strategy 1: Use fill() ---
                # fill() clears the field first and types text.
                # It also performs actionability checks (visible, enabled, etc.)
                logger.debug(f"Trying to 'fill' element {selector} (includes actionability checks)...")
                try:
                    element.fill(text, timeout=self.default_action_timeout * 0.8) # Timeout for fill action
                    logger.info(f"'fill' successful for element: {selector}")
                    self._human_like_delay(0.5, 1.0) # Delay after successful input
                    return # Success! Exit the method.
                except (PlaywrightTimeoutError, PlaywrightError) as fill_error:
                    logger.warning(f"'fill' action failed for {selector}: {fill_error}. Attempting fallback to 'type'.")
                    # Proceed to fallback if fill fails

                # --- Strategy 2: Fallback to type() ---
                # Only try type if fill failed.
                # type() simulates key presses, might work if fill had issues.
                logger.debug(f"Trying fallback 'type' for element {selector}...")
                try:
                    # We can add clear() before type if needed, but let type handle it first.
                    # element.clear(timeout=5000)
                    # self._human_like_delay(0.1, 0.3)
                    typing_delay_ms = random.uniform(80, 180)
                    element.type(text, delay=typing_delay_ms, timeout=self.default_action_timeout * 0.8)
                    logger.info(f"Fallback 'type' successful for element: {selector}")
                    self._human_like_delay(0.5, 1.0) # Delay after successful input
                    return # Success!
                except (PlaywrightTimeoutError, PlaywrightError) as type_error:
                     logger.error(f"Both 'fill' and fallback 'type' failed for {selector}. Last error ('type'): {type_error}")
                     # Raise the error from the 'type' attempt as it was the last one
                     raise type_error from fill_error # Chain the exceptions for context

            else:
                 # _find_element failed (timeout finding/attaching)
                 raise PlaywrightError(f"Element not found or not attached within timeout, cannot type: {selector}")

        # Catch errors raised from fill/type or _find_element
        except PlaywrightTimeoutError as e:
             logger.error(f"Timeout during input operation stages for selector: {selector}. Element might be obscured, disabled, or unstable.")
             raise PlaywrightTimeoutError(f"Timeout trying to input text into element: {selector}. Check interactability.") from e
        except PlaywrightError as e:
             logger.error(f"PlaywrightError inputting text into element {selector}: {e}")
             raise PlaywrightError(f"Failed to input text into element {selector}: {e}") from e
        except Exception as e:
             logger.error(f"Unexpected error inputting text into {selector}: {e}", exc_info=True)
             raise PlaywrightError(f"Unexpected error inputting text into element {selector}: {e}") from e
             
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
            raise PlaywrightError("Browser not started.")
        try:
            logger.info(f"Extracting text from selector: {selector}")
            element = self._find_element(selector) # Use helper to find first
            if not element:
                 error_msg = f"Element with selector '{selector}' could not be found for text extraction."
                 logger.error(error_msg)
                 return f"Error: {error_msg}" # Return specific error

            text = element.text_content()
            if text is None: text = "" # Handle case where text_content is None
            logger.info(f"Successfully extracted text from '{selector}': '{text[:100]}...'")
            return text.strip() # Return stripped text
        except Exception as e:
            logger.error(f"Error extracting text from '{selector}': {e}", exc_info=True)
            return f"Error extracting text: {e}" # Return specific error

    def extract_attributes(self, selector: str, attributes: List[str]) -> Dict[str, Optional[str]]:
        """
        Extracts specified attributes from the first element matching the selector.

        Args:
            selector: The Playwright selector for the element.
            attributes: A list of attribute names to extract (e.g., ['href', 'src', 'value']).

        Returns:
            A dictionary where keys are attribute names and values are the extracted
            attribute values (or None if an attribute doesn't exist).
            Returns an empty dict if the element is not found.
        """
        if not self.page:
            raise PlaywrightError("Browser not started.")
        if not attributes:
             logger.warning("extract_attributes called with empty attributes list.")
             return {}

        result_dict = {}
        try:
            logger.info(f"Extracting attributes {attributes} from selector: {selector}")
            element = self._find_element(selector) # Use helper to find first

            if not element:
                 logger.error(f"Element with selector '{selector}' not found for attribute extraction.")
                 # Return specific error indicator? Or just empty dict? Let's return dict with error marker
                 return {"error": f"Element not found: {selector}"}

            for attr_name in attributes:
                try:
                     attr_value = element.get_attribute(attr_name)
                     result_dict[attr_name] = attr_value # Store value (can be None if attr doesn't exist)
                     logger.debug(f"Extracted attribute '{attr_name}': '{str(attr_value)[:100]}...' from '{selector}'")
                except Exception as attr_e:
                     logger.warning(f"Could not extract attribute '{attr_name}' from '{selector}': {attr_e}")
                     result_dict[attr_name] = f"Error extracting: {attr_e}" # Indicate specific attribute error

            logger.info(f"Successfully extracted attributes {list(result_dict.keys())} from '{selector}'.")
            return result_dict

        except Exception as e:
            logger.error(f"Error extracting attributes {attributes} from '{selector}': {e}", exc_info=True)
            # Return dict indicating a general failure for this extraction attempt
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