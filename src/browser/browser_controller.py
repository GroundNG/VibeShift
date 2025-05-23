# /src/browser/browser_controller.py
from patchright.sync_api import sync_playwright, Page, Browser, Playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError, Response, Request, Locator, ConsoleMessage, expect
import logging
import time
import random
import json
import os
from typing import Optional, Any, Dict, List, Callable, Tuple
import threading
import platform

from ..dom.service import DomService
from ..dom.views import DOMState, DOMElementNode, SelectorMap
from .panel.panel import Panel

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

# --- JavaScript for click listener and selector generation ---
CLICK_LISTENER_JS = """
async () => {
    // Reset/Initialize the global flag/variable
  window._recorder_override_selector = undefined;
  console.log('[Recorder Listener] Attaching click listener...');
  const PANEL_ID = 'bw-recorder-panel';
  
  const clickHandler = async (event) => {
    const targetElement = event.target;
    let isPanelClick = false; // Flag to track if click is inside the panel
    
    // Check if the click target is the panel or inside the panel
    // using element.closest()
    if (targetElement && targetElement.closest && targetElement.closest(`#${PANEL_ID}`)) {
        console.log('[Recorder Listener] Click inside panel detected. Allowing event to proceed normally.');
        isPanelClick = true;
        // DO ABSOLUTELY NOTHING HERE - let the event continue to the button's own listener
    }
    
    // --- Only process as an override attempt if it was NOT a panel click ---
    if (!isPanelClick) {
        console.log('[Recorder Listener] Click detected (Outside panel)! Processing as override.');
        event.preventDefault(); // Prevent default action (like navigation) ONLY for override clicks
        event.stopPropagation(); // Stop propagation ONLY for override clicks

        if (!targetElement) {
          console.warn('[Recorder Listener] Override click event has no target.');
          // Remove listener even if target is null to avoid getting stuck
          document.body.removeEventListener('click', clickHandler, { capture: true });
          console.log('[Recorder Listener] Listener removed due to null target.');
          return;
        }
    

        // --- Simple Selector Generation (enhance as needed) ---
        let selector = '';
        function escapeCSS(value) {
            if (!value) return '';
            // Basic escape for common CSS special chars in identifiers/strings
            // For robust escaping, a library might be better, but this covers many cases.
            return value.replace(/([!"#$%&'()*+,./:;<=>?@\\[\\]^`{|}~])/g, '\\$1');
        }

        if (targetElement.id && targetElement.id !== PANEL_ID && targetElement.id !== 'playwright-highlight-container') {
        selector = `#${escapeCSS(targetElement.id.trim())}`;
        } else if (targetElement.getAttribute('data-testid')) {
        selector = `[data-testid="${escapeCSS(targetElement.getAttribute('data-testid').trim())}"]`;
        } else if (targetElement.name) {
        selector = `${targetElement.tagName.toLowerCase()}[name="${escapeCSS(targetElement.name.trim())}"]`;
        } else {
        // Fallback: Basic XPath -> CSS approximation (needs improvement)
        let path = '';
        let current = targetElement;
        while (current && current.tagName && current.tagName.toLowerCase() !== 'body' && current.parentNode) {
            let segment = current.tagName.toLowerCase();
            const parent = current.parentElement;
            if (parent) {
                const siblings = Array.from(parent.children);
                const sameTagSiblings = siblings.filter(sib => sib.tagName === current.tagName);
                if (sameTagSiblings.length > 1) {
                    let index = 1;
                    for(let i=0; i < sameTagSiblings.length; i++) { // Find index correctly
                        if(sameTagSiblings[i] === current) {
                            index = i + 1;
                            break;
                        }
                    }
                    // Prefer nth-child if possible, might be slightly more stable
                    try {
                        const siblingIndex = Array.prototype.indexOf.call(parent.children, current) + 1;
                        segment += `:nth-child(${siblingIndex})`;
                    } catch(e) { // Fallback if indexOf fails
                        segment += `:nth-of-type(${index})`;
                    }
                }
            }
            path = segment + (path ? ' > ' + path : '');
            current = parent;
        }
        selector = path ? `body > ${path}` : targetElement.tagName.toLowerCase();
        console.log(`[Recorder Listener] Generated fallback selector: ${selector}`);
        }
        // --- End Selector Generation ---

        console.log(`[Recorder Listener] Override Target: ${targetElement.tagName}, Generated selector: ${selector}`);

        // Only set override if a non-empty selector was generated
        if (selector) {
            window._recorder_override_selector = selector;
            console.log('[Recorder Listener] Override selector variable set.');
        } else {
             console.warn('[Recorder Listener] Could not generate a valid selector for the override click.');
        }
        
        // ---- IMPORTANT: Remove the listener AFTER processing an override click ----
        // This prevents it interfering further and ensures it's gone before panel interaction waits
        document.body.removeEventListener('click', clickHandler, { capture: true });
        console.log('[Recorder Listener] Listener removed after processing override click.');
    };
    // If it WAS a panel click (isPanelClick = true), we did nothing in this handler.
    // The event continues to the button's specific onclick handler.
    // The listener remains attached to the body for subsequent clicks outside the panel.
  };
  // --- Add listener ---
  // Ensure no previous listener exists before adding a new one
  if (window._recorderClickListener) {
      console.warn('[Recorder Listener] Removing potentially lingering listener before attaching new one.');
      document.body.removeEventListener('click', window._recorderClickListener, { capture: true });
  }
  // Add listener in capture phase to catch clicks first
  document.body.addEventListener('click', clickHandler, { capture: true });
  window._recorderClickListener = clickHandler; // Store reference to remove later
}
"""

REMOVE_CLICK_LISTENER_JS = """
() => {
  let removed = false;
  // Remove listener
  if (window._recorderClickListener) {
    document.body.removeEventListener('click', window._recorderClickListener, { capture: true });
    delete window._recorderClickListener;
    console.log('[Recorder Listener] Listener explicitly removed.');
    removed = true;
  } else {
    console.log('[Recorder Listener] No active listener found to remove.');
  }
  // Clean up global variable
  if (window._recorder_override_selector !== undefined) {
      delete window._recorder_override_selector;
      console.log('[Recorder Listener] Override selector variable cleaned up.');
  }
  return removed;
}
"""


class BrowserController:
    """Handles Playwright browser automation tasks, including console message capture."""

    def __init__(self, headless=True, viewport_size=None, auth_state_path: Optional[str] = None):
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: Optional[Any] = None # Keep context reference
        self.page: Page | None = None
        self.headless = headless
        self.default_navigation_timeout = 9000
        self.default_action_timeout = 9000
        self._dom_service: Optional[DomService] = None
        self.console_messages: List[Dict[str, Any]] = [] # <-- Add list to store messages
        self.viewport_size = viewport_size
        self.network_requests: List[Dict[str, Any]] = []
        self.page_performance_timing: Optional[Dict[str, Any]] = None 
        self.auth_state_path = auth_state_path
        
        self.panel = Panel(headless=headless, page=self.page)
        logger.info(f"BrowserController initialized (headless={headless}).")
        
    def _handle_response(self, response: Response):
        """Callback function to handle network responses."""
        request = response.request
        timing = request.timing
        # Calculate duration robustly
        start_time = timing.get('requestStart', -1)
        end_time = timing.get('responseEnd', -1)
        duration_ms = None
        if start_time >= 0 and end_time >= 0 and end_time >= start_time:
            duration_ms = round(end_time - start_time)
                
        req_data = {
            "url": response.url,
            "method": request.method,
            "status": response.status,
            "status_text": response.status_text,
            "start_time_ms": start_time if start_time >= 0 else None, # Use ms relative to navigationStart
            "end_time_ms": end_time if end_time >= 0 else None,     # Use ms relative to navigationStart
            "duration_ms": duration_ms,
            "resource_type": request.resource_type,
            "headers": dict(response.headers), # Store response headers
            "request_headers": dict(request.headers), # Store request headers
            # Timing breakdown (optional, can be verbose)
            # "timing_details": timing,
        }
        self.network_requests.append(req_data)
        
    def _handle_request_failed(self, request: Request):
        """Callback function to handle failed network requests."""
        try:
            failure_text = request.failure
            logger.warning(f"[NETWORK.FAILED] {request.method} {request.url} - Error: {failure_text}")
            req_data = {
                "url": request.url,
                "method": request.method,
                "status": None, # No status code available for request failure typically
                "status_text": "Request Failed",
                "start_time_ms": request.timing.get('requestStart', -1) if request.timing else None, # May still have start time
                "end_time_ms": None, # Failed before response end
                "duration_ms": None,
                "resource_type": request.resource_type,
                "headers": None, # No response headers
                "request_headers": dict(request.headers),
                "error_text": failure_text # Store the failure reason
            }
            self.network_requests.append(req_data)
        except Exception as e:
             logger.error(f"Error within _handle_request_failed for URL {request.url}: {e}", exc_info=True)

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
            # {'width': 1280, 'height': 720},
            # {'width': 1366, 'height': 768},
            {'width': 800, 'height': 600},
            # {'width': 1536, 'height': 864},
        ]
        base = random.choice(common_sizes)
        # Add small random offset
        if not self.viewport_size:
            base['width'] += random.randint(-10, 10)
            base['height'] += random.randint(-5, 5)
        else:
            base = self.viewport_size
        return base

    def _human_like_delay(self, min_secs: float, max_secs: float):
        """ Sleeps for a random duration within the specified range. """
        delay = random.uniform(min_secs, max_secs)
        logger.debug(f"Applying human-like delay: {delay:.2f} seconds")
        time.sleep(delay)
        
    def _get_locator(self, selector: str):
        """
        Gets a Playwright locator for the first matching element,
        handling potential XPath selectors passed as CSS.
        """
        if not self.page:
            raise PlaywrightError("Page is not initialized.")
        if not selector:
            raise ValueError("Selector cannot be empty.")

        # Basic check to see if it looks like XPath
        # Playwright's locator handles 'xpath=...' automatically,
        # but sometimes plain XPaths are passed. Let's try to detect them.
        is_likely_xpath = selector.startswith(('/', '(', '.')) or \
                          ('/' in selector and not any(c in selector for c in ['#', '.', '[', '>', '+', '~', '='])) # Avoid CSS chars

        processed_selector = selector
        if is_likely_xpath and not selector.startswith(('css=', 'xpath=')):
            # If it looks like XPath, explicitly prefix it for Playwright's locator
            logger.debug(f"Selector '{selector}' looks like XPath. Using explicit 'xpath=' prefix.")
            processed_selector = f"xpath={selector}"
        # If it starts with css= or xpath=, Playwright handles it.
        # Otherwise, it's assumed to be a CSS selector.

        try:
            logger.debug(f"Attempting to create locator using: '{processed_selector}'")
            # Use .first to always target a single element, consistent with other actions
            locator = self.page.locator(processed_selector).first
            return locator
        except Exception as e:
            # Catch errors during locator creation itself (e.g., invalid selector syntax)
            logger.error(f"Failed to create locator for processed selector: '{processed_selector}'. Original: '{selector}'. Error: {e}")
            # Re-raise using the processed selector in the message for clarity
            raise PlaywrightError(f"Invalid selector syntax or error creating locator: '{processed_selector}'. Error: {e}") from e
    

    # Recorder Methods =============
    def setup_click_listener(self) -> bool:
        """Injects JS to listen for the next user click and report the selector."""
        if self.headless:
             logger.error("Cannot set up click listener in headless mode.")
             return False
        if not self.page:
            logger.error("Page not initialized. Cannot set up click listener.")
            return False
        try:
            # Inject and run the listener setup JS
            # It now resets the flag internally before adding the listener
            self.page.evaluate(CLICK_LISTENER_JS)
            logger.info("JavaScript click listener attached (using pre-exposed callback).")
            return True

        except Exception as e:
            logger.error(f"Failed to set up recorder click listener: {e}", exc_info=True)
            return False

    def remove_click_listener(self) -> bool:
        """Removes the injected JS click listener."""
        if self.headless: return True # Nothing to remove
        if not self.page:
            logger.warning("Page not initialized. Cannot remove click listener.")
            return False
        try:
            removed = self.page.evaluate(REMOVE_CLICK_LISTENER_JS)

            return removed
        except Exception as e:
            logger.error(f"Failed to remove recorder click listener: {e}", exc_info=True)
            return False

    def wait_for_user_click_or_timeout(self, timeout_seconds: float) -> Optional[str]:
        """
        Waits for the user to click (triggering the callback) or for the timeout.
        Returns the selector if clicked, None otherwise.
        MUST be called after setup_click_listener.
        """
        if self.headless: return None
        if not self.page:
             logger.error("Page not initialized. Cannot wait for click function.")
             return None

        selector_result = None
        js_condition = "() => window._recorder_override_selector !== undefined"
        timeout_ms = timeout_seconds * 1000

        logger.info(f"Waiting up to {timeout_seconds}s for user click (checking JS flag)...")

        try:
            # Wait for the JS condition to become true
            self.page.wait_for_function(js_condition, timeout=timeout_ms)

            # If wait_for_function completes without timeout, the flag was set
            logger.info("User click detected (JS flag set)!")
            # Retrieve the value set by the click handler
            selector_result = self.page.evaluate("window._recorder_override_selector")
            logger.debug(f"Retrieved selector from JS flag: {selector_result}")

        except PlaywrightTimeoutError:
            logger.info("Timeout reached waiting for user click (JS flag not set).")
            selector_result = None # Timeout occurred
        except Exception as e:
             logger.error(f"Error during page.wait_for_function: {e}", exc_info=True)
             selector_result = None # Treat other errors as timeout/failure

        finally:
             # Clean up the JS listener and the flag regardless of outcome
             self.remove_click_listener()

        return selector_result


    # Highlighting elements
    def highlight_element(self, selector: str, index: int, color: str = "#FF0000", text: Optional[str] = None, node_xpath: Optional[str] = None):
        """Highlights an element using a specific selector and index label."""
        if self.headless or not self.page: return
        try:
            self.page.evaluate("""
                (args) => {
                    const { selector, index, color, text, node_xpath } = args;
                    const HIGHLIGHT_CONTAINER_ID = "bw-highlight-container"; // Unique ID

                    let container = document.getElementById(HIGHLIGHT_CONTAINER_ID);
                    if (!container) {
                        container = document.createElement("div");
                        container.id = HIGHLIGHT_CONTAINER_ID;
                        container.style.position = "fixed";
                        container.style.pointerEvents = "none";
                        container.style.top = "0";
                        container.style.left = "0";
                        container.style.width = "0"; // Occupy no space
                        container.style.height = "0";
                        container.style.zIndex = "2147483646"; // Below listener potentially
                        document.body.appendChild(container);
                    }

                    let element = null;
                    try {
                        element = document.querySelector(selector);
                    } catch (e) {
                        console.warn(`[Highlighter] querySelector failed for '${selector}': ${e.message}.`);
                        element = null; // Ensure element is null if querySelector fails
                    }

                    // --- Fallback to XPath if CSS failed AND xpath is available ---
                    if (!element && node_xpath) {
                        console.log(`[Highlighter] Falling back to XPath: ${node_xpath}`);
                        try {
                            element = document.evaluate(
                                node_xpath,
                                document,
                                null,
                                XPathResult.FIRST_ORDERED_NODE_TYPE,
                                null
                            ).singleNodeValue;
                        } catch (e) {
                            console.error(`[Highlighter] XPath evaluation failed for '${node_xpath}': ${e.message}`);
                            element = null;
                        }
                    }
                    // ------------------------------------------------------------

                    if (!element) {
                        console.warn(`[Highlighter] Element not found using selector '${selector}' or XPath '${node_xpath}'. Cannot highlight.`);
                        return;
                    }

                    const rect = element.getBoundingClientRect();
                    if (!rect || rect.width === 0 || rect.height === 0) return; // Don't highlight non-rendered

                    const overlay = document.createElement("div");
                    overlay.style.position = "fixed";
                    overlay.style.border = `2px solid ${color}`;
                    overlay.style.backgroundColor = color + '1A'; // 10% opacity
                    overlay.style.pointerEvents = "none";
                    overlay.style.boxSizing = "border-box";
                    overlay.style.top = `${rect.top}px`;
                    overlay.style.left = `${rect.left}px`;
                    overlay.style.width = `${rect.width}px`;
                    overlay.style.height = `${rect.height}px`;
                    overlay.style.zIndex = "2147483646";
                    overlay.setAttribute('data-highlight-selector', selector); // Mark for cleanup
                    container.appendChild(overlay);

                    const label = document.createElement("div");
                    const labelText = text ? `${index}: ${text}` : `${index}`;
                    label.style.position = "fixed";
                    label.style.background = color;
                    label.style.color = "white";
                    label.style.padding = "1px 4px";
                    label.style.borderRadius = "4px";
                    label.style.fontSize = "10px";
                    label.style.fontWeight = "bold";
                    label.style.zIndex = "2147483647";
                    label.textContent = labelText;
                    label.setAttribute('data-highlight-selector', selector); // Mark for cleanup

                    // Position label top-left, slightly offset
                    let labelTop = rect.top - 18;
                    let labelLeft = rect.left;
                     // Adjust if label would go off-screen top
                    if (labelTop < 0) labelTop = rect.top + 2;

                    label.style.top = `${labelTop}px`;
                    label.style.left = `${labelLeft}px`;
                    container.appendChild(label);
                }
            """, {"selector": selector, "index": index, "color": color, "text": text, "node_xpath": node_xpath})
        except Exception as e:
            logger.warning(f"Failed to highlight element '{selector}': {e}")

    def clear_highlights(self):
        """Removes all highlight overlays and labels added by highlight_element."""
        if self.headless or not self.page: return
        try:
            self.page.evaluate("""
                () => {
                    const container = document.getElementById("bw-highlight-container");
                    if (container) {
                        container.innerHTML = ''; // Clear contents efficiently
                    }
                }
            """)
            # logger.debug("Cleared highlights.")
        except Exception as e:
            logger.warning(f"Could not clear highlights: {e}")


    # Getters
    def get_structured_dom(self, highlight_all_clickable_elements: bool = True, viewport_expansion: int = 0) -> Optional[DOMState]:
        """
        Uses DomService to get a structured representation of the interactive DOM elements.

        Args:
            highlight_all_clickable_elements: Whether to visually highlight elements in the browser.
            viewport_expansion: Pixel value to expand the viewport for element detection (0=viewport only, -1=all).

        Returns:
            A DOMState object containing the element tree and selector map, or None on error.
        """
        highlight_all_clickable_elements = False # SETTING TO FALSE TO AVOID CONFUSION WITH NEXT ACTION HIGHLIGHT
        
        if not self.page:
            logger.error("Browser/Page not initialized or DomService unavailable.")
            return None
        if not self._dom_service:
            self._dom_service = DomService(self.page)


        # --- RECORDER MODE: Never highlight via JS during DOM build ---
        # Highlighting is done separately by BrowserController.highlight_element
        if self.headless == False: # Assume non-headless is recorder mode context
             highlight_all_clickable_elements = False
        # --- END RECORDER MODE ---
        
        if not self._dom_service:
            logger.error("DomService unavailable.")
            return None

        try:
            logger.info(f"Requesting structured DOM (highlight={highlight_all_clickable_elements}, expansion={viewport_expansion})...")
            start_time = time.time()
            dom_state = self._dom_service.get_clickable_elements(
                highlight_elements=highlight_all_clickable_elements,
                focus_element=-1, # Not focusing on a specific element for now
                viewport_expansion=viewport_expansion
            )
            end_time = time.time()
            logger.info(f"Structured DOM retrieved in {end_time - start_time:.2f}s. Found {len(dom_state.selector_map)} interactive elements.")
            # Generate selectors immediately for recorder use
            if dom_state and dom_state.selector_map:
                for node in dom_state.selector_map.values():
                     if not node.css_selector:
                           node.css_selector = self.get_selector_for_node(node)
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
    
    def get_performance_timing(self) -> Optional[Dict[str, Any]]:
        """Gets the window.performance.timing object from the page."""
        if not self.page:
            logger.error("Cannot get performance timing, page not initialized.")
            return None
        try:
            # Evaluate script to get the performance timing object as JSON
            timing_json = self.page.evaluate("() => JSON.stringify(window.performance.timing)")
            if timing_json:
                self.page_performance_timing = json.loads(timing_json) # Store it
                logger.debug("Retrieved window.performance.timing.")
                return self.page_performance_timing
            else:
                logger.warning("window.performance.timing unavailable or empty.")
                return None
        except Exception as e:
            logger.error(f"Error getting performance timing: {e}", exc_info=True)
            return None

    def get_current_url(self) -> str:
        """Returns the current URL of the page."""
        if not self.page:
            return "Error: Browser not started."
        try:
            return self.page.url
        except Exception as e:
            logger.error(f"Error getting current URL: {e}", exc_info=True)
            return f"Error retrieving URL: {e}"

    def get_browser_version(self) -> str:
        if not self.browser:
            return "Unknown"
        try:
            # Browser version might be available directly
            return f"{self.browser.browser_type.name} {self.browser.version}"
        except Exception:
            logger.warning("Could not retrieve exact browser version.")
            return self.browser.browser_type.name if self.browser else "Unknown"

    def get_os_info(self) -> str:
        try:
            return f"{platform.system()} {platform.release()}"
        except Exception:
            logger.warning("Could not retrieve OS information.")
            return "Unknown"

    def get_viewport_size(self) -> Optional[Dict[str, int]]:
         if not self.page:
              return None
         try:
              return self.page.viewport_size # Returns {'width': W, 'height': H} or None
         except Exception:
             logger.warning("Could not retrieve viewport size.")
             return None


    def get_console_messages(self) -> List[Dict[str, Any]]:
        """Returns a copy of the captured console messages."""
        return list(self.console_messages) # Return a copy

    def clear_console_messages(self):
        """Clears the stored console messages."""
        logger.debug("Clearing captured console messages.")
        self.console_messages = []
        
    def get_network_requests(self) -> List[Dict[str, Any]]:
        """Returns a copy of the captured network request data."""
        return list(self.network_requests)

    def clear_network_requests(self):
        """Clears the stored network request data."""
        logger.debug("Clearing captured network requests.")
        self.network_requests = []



    def validate_assertion(self, assertion_type: str, selector: str, params: Dict[str, Any], timeout_ms: int = 3000) -> Tuple[bool, Optional[str]]:
        """
        Performs a quick Playwright check to validate a proposed assertion. 

        Args:
            assertion_type: The type of assertion (e.g., 'assert_visible').
            selector: The CSS selector for the target element.
            params: Dictionary of parameters for the assertion (e.g., expected_text).
            timeout_ms: Short timeout for the validation check.

        Returns:
            Tuple (bool, Optional[str]): (True, None) if validation passes,
                                         (False, error_message) if validation fails.
        """
        if not self.page:
            return False, "Page not initialized."
        if not selector:
            # Assertions like 'assert_llm_verification' might not have a selector
            if assertion_type == 'assert_llm_verification':
                logger.info("Skipping validation for 'assert_llm_verification' as it relies on external LLM check.")
                return True, None
            return False, "Selector is required for validation."
        if not assertion_type:
            return False, "Assertion type is required for validation."

        logger.info(f"Validating assertion: {assertion_type} on '{selector}' with params {params} (timeout: {timeout_ms}ms)")
        try:
            locator = self._get_locator(selector) # Use helper to handle xpath/css

            # Use Playwright's expect() for efficient checks
            if assertion_type == 'assert_visible':
                expect(locator).to_be_visible(timeout=timeout_ms)
            elif assertion_type == 'assert_hidden':
                expect(locator).to_be_hidden(timeout=timeout_ms)
            elif assertion_type == 'assert_text_equals':
                expected_text = params.get('expected_text')
                if expected_text is None: return False, "Missing 'expected_text' parameter for assert_text_equals"
                expect(locator).to_have_text(expected_text, timeout=timeout_ms)
            elif assertion_type == 'assert_text_contains':
                expected_text = params.get('expected_text')
                if expected_text is None: return False, "Missing 'expected_text' parameter for assert_text_contains"
                expect(locator).to_contain_text(expected_text, timeout=timeout_ms)
            elif assertion_type == 'assert_attribute_equals':
                attr_name = params.get('attribute_name')
                expected_value = params.get('expected_value')
                if not attr_name: return False, "Missing 'attribute_name' parameter"
                # Note: Playwright's to_have_attribute handles presence and value check
                expect(locator).to_have_attribute(attr_name, expected_value if expected_value is not None else "", timeout=timeout_ms) # Check empty string if value is None/missing? Or require value? Let's require non-None value.
                # if expected_value is None: return False, "Missing 'expected_value' parameter" # Stricter check
                # expect(locator).to_have_attribute(attr_name, expected_value, timeout=timeout_ms)
            elif assertion_type == 'assert_element_count':
                expected_count = params.get('expected_count')
                if expected_count is None: return False, "Missing 'expected_count' parameter"
                # Re-evaluate locator to get all matches for count
                all_matches_locator = self.page.locator(selector)
                expect(all_matches_locator).to_have_count(expected_count, timeout=timeout_ms)
            elif assertion_type == 'assert_checked':
                expect(locator).to_be_checked(timeout=timeout_ms)
            elif assertion_type == 'assert_not_checked':
                 # Use expect(...).not_to_be_checked()
                 expect(locator).not_to_be_checked(timeout=timeout_ms)
            elif assertion_type == 'assert_enabled':
                 expect(locator).to_be_enabled(timeout=timeout_ms)
            elif assertion_type == 'assert_disabled':
                 expect(locator).to_be_disabled(timeout=timeout_ms)
            elif assertion_type == 'assert_llm_verification':
                 logger.info("Skipping Playwright validation for 'assert_llm_verification'.")
                 # This assertion type is validated externally by the LLM during execution.
                 pass # Treat as passed for this quick check
            else:
                return False, f"Unsupported assertion type for validation: {assertion_type}"

            # If no exception was raised by expect()
            logger.info(f"Validation successful for {assertion_type} on '{selector}'.")
            return True, None

        except PlaywrightTimeoutError as e:
            err_msg = f"Validation failed for {assertion_type} on '{selector}': Timeout ({timeout_ms}ms) - {str(e).splitlines()[0]}"
            logger.warning(err_msg)
            return False, err_msg
        except AssertionError as e: # Catch expect() assertion failures
            err_msg = f"Validation failed for {assertion_type} on '{selector}': Condition not met - {str(e).splitlines()[0]}"
            logger.warning(err_msg)
            return False, err_msg
        except PlaywrightError as e:
            err_msg = f"Validation failed for {assertion_type} on '{selector}': PlaywrightError - {str(e).splitlines()[0]}"
            logger.warning(err_msg)
            return False, err_msg
        except Exception as e:
            err_msg = f"Unexpected error during validation for {assertion_type} on '{selector}': {type(e).__name__} - {e}"
            logger.error(err_msg, exc_info=True)
            return False, err_msg



    def goto(self, url: str):
        """Navigates the page to a specific URL."""
        if not self.page:
            raise PlaywrightError("Browser not started. Call start() first.")
        try:
            logger.info(f"Navigating to URL: {url}")
            # Use default navigation timeout set in context
            response = self.page.goto(url, wait_until='load', timeout=self.default_navigation_timeout) 
            # Add a small stable delay after load
            time.sleep(1)
            status = response.status if response else 'unknown'
            
            # --- Capture performance timing after navigation ---
            self.get_performance_timing()
            
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

    def check(self, selector: str): 
        """Checks a checkbox or radio button."""
        if not self.page:
            raise PlaywrightError("Browser not started.")
        try:
            logger.info(f"Attempting to check element: {selector}")
            locator = self.page.locator(selector).first
            # check() includes actionability checks (visible, enabled)
            locator.check(timeout=self.default_action_timeout)
            logger.info(f"Checked element: {selector}")
            self._human_like_delay(0.2, 0.5) # Small delay after checking
        except PlaywrightTimeoutError as e:
            logger.error(f"Timeout ({self.default_action_timeout}ms) waiting for element '{selector}' to be actionable for check.")
            # Add screenshot on failure
            screenshot_path = f"output/check_timeout_{selector.replace(' ','_').replace(':','_').replace('>','_')[:30]}_{int(time.time())}.png"
            self.save_screenshot(screenshot_path)
            logger.error(f"Saved screenshot on check timeout to: {screenshot_path}")
            raise PlaywrightTimeoutError(f"Timeout trying to check element: '{selector}'. Check visibility and enabled state. Screenshot: {screenshot_path}") from e
        except PlaywrightError as e:
            logger.error(f"PlaywrightError checking element '{selector}': {e}")
            raise PlaywrightError(f"Failed to check element '{selector}': {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error checking '{selector}': {e}", exc_info=True)
            raise PlaywrightError(f"Unexpected error checking element '{selector}': {e}") from e

    def uncheck(self, selector: str):
        """Unchecks a checkbox."""
        if not self.page:
            raise PlaywrightError("Browser not started.")
        try:
            logger.info(f"Attempting to uncheck element: {selector}")
            locator = self.page.locator(selector).first
            # uncheck() includes actionability checks
            locator.uncheck(timeout=self.default_action_timeout)
            logger.info(f"Unchecked element: {selector}")
            self._human_like_delay(0.2, 0.5) # Small delay
        except PlaywrightTimeoutError as e:
            logger.error(f"Timeout ({self.default_action_timeout}ms) waiting for element '{selector}' to be actionable for uncheck.")
            screenshot_path = f"output/uncheck_timeout_{selector.replace(' ','_').replace(':','_').replace('>','_')[:30]}_{int(time.time())}.png"
            self.save_screenshot(screenshot_path)
            logger.error(f"Saved screenshot on uncheck timeout to: {screenshot_path}")
            raise PlaywrightTimeoutError(f"Timeout trying to uncheck element: '{selector}'. Screenshot: {screenshot_path}") from e
        except PlaywrightError as e:
            logger.error(f"PlaywrightError unchecking element '{selector}': {e}")
            raise PlaywrightError(f"Failed to uncheck element '{selector}': {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error unchecking '{selector}': {e}", exc_info=True)
            raise PlaywrightError(f"Unexpected error unchecking element '{selector}': {e}") from e
        
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
                if not self.headless: time.sleep(0.2)
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
                typing_delay_ms = random.uniform(90, 180)
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
        
    def press(self, selector: str, keys: str):
        """Presses key(s) on a specific element."""
        if not self.page:
            raise PlaywrightError("Browser not started.")
        try:
            logger.info(f"Attempting to press '{keys}' on element: {selector}")
            locator = self._get_locator(selector)
            # Ensure element is actionable first (visible, enabled) before pressing
            expect(locator).to_be_enabled(timeout=self.default_action_timeout / 2) # Quick check
            expect(locator).to_be_visible(timeout=self.default_action_timeout / 2)
            locator.press(keys, timeout=self.default_action_timeout)
            logger.info(f"Pressed '{keys}' on element: {selector}")
            self._human_like_delay(0.2, 0.6) # Small delay after key press
        except (PlaywrightTimeoutError, PlaywrightError, AssertionError) as e: # Catch expect failures too
            error_msg = f"Timeout or error pressing '{keys}' on element '{selector}': {type(e).__name__} - {e}"
            logger.error(error_msg)
            screenshot_path = f"output/press_fail_{selector.replace(' ','_').replace(':','_').replace('>','_')[:30]}_{int(time.time())}.png"
            self.save_screenshot(screenshot_path)
            logger.error(f"Saved screenshot on press failure to: {screenshot_path}")
            raise PlaywrightError(f"{error_msg}. Screenshot: {screenshot_path}") from e
        except Exception as e:
            logger.error(f"Unexpected error pressing '{keys}' on '{selector}': {e}", exc_info=True)
            raise PlaywrightError(f"Unexpected error pressing '{keys}' on element '{selector}': {e}") from e

    def drag_and_drop(self, source_selector: str, target_selector: str):
        """Drags an element defined by source_selector to an element defined by target_selector."""
        if not self.page:
            raise PlaywrightError("Browser not started.")
        try:
            logger.info(f"Attempting to drag '{source_selector}' to '{target_selector}'")
            source_locator = self._get_locator(source_selector)
            target_locator = self._get_locator(target_selector)

            # Optional: Check visibility/existence before drag attempt
            expect(source_locator).to_be_visible(timeout=self.default_action_timeout / 2)
            expect(target_locator).to_be_visible(timeout=self.default_action_timeout / 2)

            # Perform drag_to with default timeout
            source_locator.drag_to(target_locator, timeout=self.default_action_timeout)
            logger.info(f"Successfully dragged '{source_selector}' to '{target_selector}'")
            self._human_like_delay(0.5, 1.2) # Delay after drag/drop
        except (PlaywrightTimeoutError, PlaywrightError, AssertionError) as e:
            error_msg = f"Timeout or error dragging '{source_selector}' to '{target_selector}': {type(e).__name__} - {e}"
            logger.error(error_msg)
            screenshot_path = f"output/drag_fail_{source_selector.replace(' ','_')[:20]}_{target_selector.replace(' ','_')[:20]}_{int(time.time())}.png"
            self.save_screenshot(screenshot_path)
            logger.error(f"Saved screenshot on drag failure to: {screenshot_path}")
            raise PlaywrightError(f"{error_msg}. Screenshot: {screenshot_path}") from e
        except Exception as e:
            logger.error(f"Unexpected error dragging '{source_selector}' to '{target_selector}': {e}", exc_info=True)
            raise PlaywrightError(f"Unexpected error dragging '{source_selector}' to '{target_selector}': {e}") from e

    def wait(self,
             timeout_seconds: Optional[float] = None,
             selector: Optional[str] = None,
             state: Optional[str] = None, # 'visible', 'hidden', 'enabled', 'disabled', 'attached', 'detached'
             url: Optional[str] = None, # String, regex, or function
            ):
        """Performs various types of waits based on provided parameters."""
        if not self.page:
            raise PlaywrightError("Browser not started.")
 
        try:
            if timeout_seconds is not None and selector is None and state is None and url is None:
                # Simple time wait
                logger.info(f"Waiting for {timeout_seconds:.2f} seconds...")
                self.page.wait_for_timeout(timeout_seconds * 1000)
                logger.info(f"Wait finished after {timeout_seconds:.2f} seconds.")
 
            elif selector and state:
                # Wait for element state
                wait_timeout = self.default_action_timeout # Use default action timeout for element waits
                logger.info(f"Waiting for element '{selector}' to be '{state}' (max {wait_timeout}ms)...")
                locator = self._get_locator(selector) # Handles potential errors
                locator.wait_for(state=state, timeout=wait_timeout)
                logger.info(f"Wait finished: Element '{selector}' is now '{state}'.")
 
            elif url:
                # Wait for URL
                wait_timeout = self.default_navigation_timeout # Use navigation timeout for URL waits
                logger.info(f"Waiting for URL matching '{url}' (max {wait_timeout}ms)...")
                self.page.wait_for_url(url, timeout=wait_timeout)
                logger.info(f"Wait finished: URL now matches '{url}'.")
 
            else:
                logger.info(f"Waiting for 5 seconds...")
                self.page.wait_for_timeout(5 * 1000)
                logger.info(f"Wait finished after {5:.2f} seconds.")
 
            # Optional small delay after successful condition wait
            if selector or url:
                self._human_like_delay(0.1, 0.3)
 
            return {"success": True, "message": "Wait condition met successfully."}
 
        except PlaywrightTimeoutError as e:
            error_msg = f"Timeout waiting for condition: {e}"
            logger.error(error_msg)
            # Don't save screenshot for wait timeouts usually, unless specifically needed
            return {"success": False, "message": error_msg}
        except (PlaywrightError, ValueError) as e:
            error_msg = f"Error during wait: {type(e).__name__}: {e}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}
        except Exception as e:
            error_msg = f"Unexpected error during wait: {e}"
            logger.error(error_msg, exc_info=True)
            return {"success": False, "message": error_msg}
            


    def start(self):
        """Starts Playwright, launches browser, creates context/page, and attaches console listener."""
        try:
            logger.info("Starting Playwright...")
            self.playwright = sync_playwright().start()
            # Consider adding args for anti-detection if needed:
            browser_args = ['--disable-blink-features=AutomationControlled']
            self.browser = self.playwright.chromium.launch(headless=self.headless, args=browser_args)
            # self.browser = self.playwright.chromium.launch(headless=self.headless)

            context_options = self.browser.new_context(
                 user_agent=self._get_random_user_agent(),
                 viewport=self._get_random_viewport(),
                 ignore_https_errors=True,
                 java_script_enabled=True,
                 extra_http_headers=COMMON_HEADERS,
            )
            context_options = {
                 "user_agent": self._get_random_user_agent(),
                 "viewport": self._get_random_viewport(),
                 "ignore_https_errors": True,
                 "java_script_enabled": True,
                 "extra_http_headers": COMMON_HEADERS,
            }

            loaded_state = False
            if self.auth_state_path and os.path.exists(self.auth_state_path):
                try:
                    logger.info(f"Attempting to load authentication state from: {self.auth_state_path}")
                    context_options["storage_state"] = self.auth_state_path
                    loaded_state = True
                except Exception as e:
                     logger.error(f"Failed to load storage state from '{self.auth_state_path}': {e}. Proceeding without saved state.", exc_info=True)
                     # Remove the invalid option if loading failed
                     if "storage_state" in context_options:
                         del context_options["storage_state"]
            elif self.auth_state_path:
                logger.warning(f"Authentication state file not found at '{self.auth_state_path}'. Proceeding without saved state. Run generation script if needed.")
            else:
                logger.info("No authentication state path provided. Proceeding without saved state.")
                
            self.context = self.browser.new_context(**context_options)
            
            self.context.set_default_navigation_timeout(self.default_navigation_timeout)
            self.context.set_default_timeout(self.default_action_timeout)
            self.context.add_init_script(HIDE_WEBDRIVER_SCRIPT)

            self.page = self.context.new_page()

            # Initialize DomService with the created page
            self._dom_service = DomService(self.page) # Instantiate here
            
            # --- Attach Console Listener ---
            self.page.on('console', self._handle_console_message)
            logger.info("Attached console message listener.")
            self.page.on('response', self._handle_response) # <<< Attach network listener
            logger.info("Attached network response listener.")
            self.page.on('requestfailed', self._handle_request_failed)
            logger.info("Attached network failed listener.")
            self.panel.inject_recorder_ui_scripts() # inject recorder ui
            
            # -----------------------------
            logger.info("Browser context and page created.")

        except Exception as e:
            logger.error(f"Failed to start Playwright or launch browser: {e}", exc_info=True)
            self.close() # Ensure cleanup on failure
            raise
    
    def close(self):
        """Closes the browser and stops Playwright."""
        self.panel.remove_recorder_panel()
        self.remove_click_listener() 
        try:
            if self.page and not self.page.is_closed():
                try:
                    self.page.remove_listener('response', self._handle_response) # <<< Remove network listener
                    logger.debug("Removed network response listener.")
                except Exception as e: logger.warning(f"Could not remove response listener: {e}")
                try:
                    self.page.remove_listener('console', self._handle_console_message)
                    logger.debug("Removed console message listener.")
                except Exception as e: logger.warning(f"Could not remove console listener: {e}")
                try:
                     self.page.remove_listener('requestfailed', self._handle_request_failed) # <<< Remove requestfailed listener
                     logger.debug("Removed network requestfailed listener.")
                except Exception as e: logger.warning(f"Could not remove requestfailed listener: {e}")
            self._dom_service = None
            if self.page and not self.page.is_closed():
                # logger.debug("Closing page...") # Added for clarity
                self.page.close()
                # logger.debug("Page closed.")
            else:
                logger.debug("Page already closed or not initialized.")
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
            self.network_requests = [] # Clear network data on final close
            self._recorder_ui_injected = False
