# File: record_auth_state_selectors.py
import time
import os
import logging
import getpass
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field

from patchright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

# Import necessary components from your project structure
from ..browser.browser_controller import BrowserController
from ..llm.llm_client import LLMClient # Assuming you have this initialized
from ..dom.views import DOMState # To type hint DOM state

# Configure basic logging for this script
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# Generic descriptions for LLM to find elements
USERNAME_FIELD_DESC = "the username input field"
PASSWORD_FIELD_DESC = "the password input field"
SUBMIT_BUTTON_DESC = "the login or submit button"
# Element to verify login success
LOGIN_SUCCESS_SELECTOR_DESC = "the logout button or link" # Description for verification element

# --- Output file path ---
AUTH_STATE_FILE = "auth_state.json"
# ---------------------

# --- Pydantic Schema for LLM Selector Response ---
class LLMSelectorResponse(BaseModel):
    selector: Optional[str] = Field(..., description="The best CSS selector found for the described element, or null if not found/identifiable.")
    reasoning: str = Field(..., description="Explanation for the chosen selector or why none was found.")
# -----------------------------------------------

# --- Helper Function to Find Selector via LLM ---
def find_element_selector_via_llm(
    llm_client: LLMClient,
    element_description: str,
    dom_state: Optional[DOMState],
    page: Any # Playwright Page object for validation
) -> Optional[str]:
    """
    Uses LLM to find a selector for a described element based on DOM context.
    Validates the selector before returning.
    """
    if not llm_client:
        logger.error("LLMClient is not available.")
        return None
    if not dom_state or not dom_state.element_tree:
        logger.error(f"Cannot find selector for '{element_description}': DOM state is not available.")
        return None

    try:
        dom_context_str, _ = dom_state.element_tree.generate_llm_context_string(context_purpose='verification')
        current_url = page.url if page else "Unknown"

        prompt = f"""
You are an AI assistant identifying CSS selectors for web automation.
Based on the following HTML context and the element description, provide the most robust CSS selector.

**Current URL:** {current_url}
**Element to Find:** "{element_description}"

**HTML Context (Visible elements, interactive `[index]`, static `(Static)`):**
```html
{dom_context_str}
\```

**Your Task:**
1. Analyze the HTML context to find the single element that best matches the description "{element_description}".
2. Provide the most stable and specific CSS selector for that element. Prioritize IDs, unique data attributes (like data-testid), or name attributes. Avoid relying solely on text or highly dynamic classes if possible.
3. If no suitable element is found, return null for the selector.

**Output Format:** Respond ONLY with a JSON object matching the following schema:
```json
{{
  "selector": "YOUR_SUGGESTED_CSS_SELECTOR_OR_NULL",
  "reasoning": "Explain your choice or why none was found."
}}
\```
"""
        logger.debug(f"Sending prompt to LLM to find selector for: '{element_description}'")
        response_obj = llm_client.generate_json(LLMSelectorResponse, prompt)

        if isinstance(response_obj, LLMSelectorResponse):
            selector = response_obj.selector
            reasoning = response_obj.reasoning
            if selector:
                logger.info(f"LLM suggested selector '{selector}' for '{element_description}'. Reasoning: {reasoning}")
                # --- Validate Selector ---
                try:
                    handles = page.query_selector_all(selector)
                    count = len(handles)
                    if count == 1:
                        logger.info(f"✅ Validation PASSED: Selector '{selector}' uniquely found the element.")
                        return selector
                    elif count > 1:
                        logger.warning(f"⚠️ Validation WARNING: Selector '{selector}' matched {count} elements. Using the first one.")
                        return selector # Still return it, maybe it's okay
                    else: # count == 0
                        logger.error(f"❌ Validation FAILED: Selector '{selector}' did not find any elements.")
                        return None
                except Exception as validate_err:
                    logger.error(f"❌ Validation ERROR for selector '{selector}': {validate_err}")
                    return None
                # --- End Validation ---
            else:
                logger.error(f"LLM could not find a selector for '{element_description}'. Reasoning: {reasoning}")
                return None
        elif isinstance(response_obj, str): # LLM Error string
             logger.error(f"LLM returned an error finding selector for '{element_description}': {response_obj}")
             return None
        else:
            logger.error(f"Unexpected response type from LLM finding selector for '{element_description}': {type(response_obj)}")
            return None

    except Exception as e:
        logger.error(f"Error during LLM selector identification for '{element_description}': {e}", exc_info=True)
        return None
# --- End Helper Function ---


# --- Main Function ---
def record_selectors_and_save_auth_state(llm_client: LLMClient, login_url: str, auth_state_file: str = AUTH_STATE_FILE):
    """
    Uses LLM to find login selectors, gets credentials securely, performs login,
    and saves the authentication state.
    """
    logger.info("--- Authentication State Generation (Recorder-Assisted Selectors) ---")

    if not login_url:
        logger.error(f"Login url not provided. Exiting...")
        return False
    
    # Get credentials securely first
    try:
        username = input(f"Enter username (will be visible): ")
        if not username: raise ValueError("Username cannot be empty.")
        password = getpass.getpass(f"Enter password for '{username}' (input will be hidden): ")
        if not password: raise ValueError("Password cannot be empty.")
    except (EOFError, ValueError) as e:
        logger.error(f"\n❌ Input error: {e}. Aborting.")
        return False
    except Exception as e:
        logger.error(f"\n❌ Error reading input: {e}")
        return False

    logger.info("Initializing BrowserController (visible browser)...")
    # Must run non-headless for user interaction/visibility AND selector validation
    browser_controller = BrowserController(headless=False)
    final_success = False

    try:
        browser_controller.start()
        page = browser_controller.page
        if not page: raise RuntimeError("Failed to initialize browser page.")

        logger.info(f"Navigating browser to login page: {login_url}")
        browser_controller.goto(login_url)

        logger.info("Attempting to identify login form selectors using LLM...")
        # Give the page a moment to settle before getting DOM
        time.sleep(1)
        dom_state = browser_controller.get_structured_dom(highlight_all_clickable_elements=False, viewport_expansion=-1)

        # Find Selectors using the helper function
        username_selector = find_element_selector_via_llm(llm_client, USERNAME_FIELD_DESC, dom_state, page)
        if not username_selector: return False # Abort if not found

        password_selector = find_element_selector_via_llm(llm_client, PASSWORD_FIELD_DESC, dom_state, page)
        if not password_selector: return False

        submit_selector = find_element_selector_via_llm(llm_client, SUBMIT_BUTTON_DESC, dom_state, page)
        if not submit_selector: return False

        logger.info("Successfully identified all necessary login selectors.")
        logger.info(f"  Username Field: '{username_selector}'")
        logger.info(f"  Password Field: '{password_selector}'")
        logger.info(f"  Submit Button:  '{submit_selector}'")

        input("\n-> Press Enter to proceed with login using these selectors and your credentials...")

        # --- Execute Login (using identified selectors and secure credentials) ---
        logger.info(f"Typing username into: {username_selector}")
        browser_controller.type(username_selector, username)
        time.sleep(0.3)

        logger.info(f"Typing password into: {password_selector}")
        browser_controller.type(password_selector, password)
        time.sleep(0.3)

        logger.info(f"Clicking submit button: {submit_selector}")
        browser_controller.click(submit_selector)

        # --- Verify Login Success ---
        logger.info("Attempting to identify login success element selector using LLM...")
        # Re-fetch DOM state after potential page change/update
        time.sleep(1) # Wait briefly for page update
        post_login_dom_state = browser_controller.get_structured_dom(highlight_all_clickable_elements=False, viewport_expansion=-1)
        login_success_selector = find_element_selector_via_llm(llm_client, LOGIN_SUCCESS_SELECTOR_DESC, post_login_dom_state, page)

        if not login_success_selector:
            logger.error("❌ Login Verification Failed: Could not identify the confirmation element via LLM.")
            raise RuntimeError("Failed to identify login confirmation element.") # Treat as failure

        logger.info(f"Waiting for login confirmation element ({login_success_selector}) to appear...")
        try:
            page.locator(login_success_selector).wait_for(state="visible", timeout=15000)
            logger.info("✅ Login successful! Confirmation element found.")
        except PlaywrightTimeoutError:
            logger.error(f"❌ Login Failed: Confirmation element '{login_success_selector}' did not appear within timeout.")
            raise # Re-raise to be caught by the main handler

        # --- Save the storage state ---
        if browser_controller.context:
            logger.info(f"Saving authentication state to {auth_state_file}...")
            browser_controller.context.storage_state(path=auth_state_file)
            logger.info(f"✅ Successfully saved authentication state.")
            final_success = True
        else:
            logger.error("❌ Cannot save state: Browser context is not available.")

    except (PlaywrightError, ValueError, RuntimeError) as e:
        logger.error(f"❌ An error occurred: {type(e).__name__}: {e}", exc_info=False)
        if browser_controller and browser_controller.page:
            ts = time.strftime("%Y%m%d_%H%M%S")
            fail_path = f"output/record_auth_error_{ts}.png"
            browser_controller.save_screenshot(fail_path)
            logger.info(f"Saved error screenshot to: {fail_path}")
    except Exception as e:
        logger.critical(f"❌ An unexpected critical error occurred: {e}", exc_info=True)
    finally:
        logger.info("Closing browser...")
        if browser_controller:
            browser_controller.close()

    return final_success
# --- End Main Function ---

