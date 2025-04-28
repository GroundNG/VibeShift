# /src/crawler_agent.py
import logging
import time
from urllib.parse import urlparse, urljoin
from typing import List, Set, Dict, Any, Optional
import asyncio # For potential async operations if BrowserController becomes async
import re
import os
import json

from pydantic import BaseModel, Field

# Use relative imports within the package if applicable, or adjust paths
from ..browser.browser_controller import BrowserController
from ..llm.llm_client import LLMClient

logger = logging.getLogger(__name__)

# --- Pydantic Schema for LLM Response ---
class SuggestedTestStepsSchema(BaseModel):
    """Schema for suggested test steps relevant to the current page."""
    suggested_test_steps: List[str] = Field(..., description="List of 3-5 specific, actionable test step descriptions (like 'Click button X', 'Type Y into Z', 'Verify text A') relevant to the current page context.")
    reasoning: str = Field(..., description="Brief reasoning for why these steps are relevant to the page content and URL.")

# --- Crawler Agent ---
class CrawlerAgent:
    """
    Crawls a given domain, identifies unique pages, and uses an LLM
    to suggest potential test flows for each discovered page.
    """

    def __init__(self, llm_client: LLMClient, headless: bool = True, politeness_delay_sec: float = 1.0):
        self.llm_client = llm_client
        self.headless = headless
        self.politeness_delay = politeness_delay_sec
        self.browser_controller: Optional[BrowserController] = None

        # State for crawling
        self.base_domain: Optional[str] = None
        self.queue: List[str] = []
        self.visited_urls: Set[str] = set()
        self.discovered_steps: Dict[str, List[str]] = {}

    def _normalize_url(self, url: str) -> str:
        """Removes fragments and trailing slashes for consistent URL tracking."""
        parsed = urlparse(url)
        # Rebuild without fragment, ensure path starts with / if root
        path = parsed.path if parsed.path else '/'
        if path.endswith('/'):
             path = path[:-1] # Remove trailing slash unless it's just '/'
        # Ensure scheme and netloc are present
        scheme = parsed.scheme if parsed.scheme else 'http' # Default to http if missing? Or raise error? Let's default.
        netloc = parsed.netloc
        if not netloc:
             logger.warning(f"URL '{url}' missing network location (domain). Skipping.")
             return None # Invalid URL for crawling
        # Query params are usually important, keep them
        query = parsed.query
        # Reconstruct
        rebuilt_url = f"{scheme}://{netloc}{path}"
        if query:
            rebuilt_url += f"?{query}"
        return rebuilt_url.lower() # Use lowercase for comparison

    def _get_domain(self, url: str) -> Optional[str]:
        """Extracts the network location (domain) from a URL."""
        try:
            return urlparse(url).netloc.lower()
        except Exception:
            return None

    def _is_valid_url(self, url: str) -> bool:
        """Basic check if a URL seems valid for crawling."""
        try:
            parsed = urlparse(url)
            # Must have scheme (http/https) and netloc (domain)
            return all([parsed.scheme in ['http', 'https'], parsed.netloc])
        except Exception:
            return False

    def _extract_links(self, current_url: str) -> Set[str]:
        """Extracts and normalizes unique, valid links from the current page."""
        if not self.browser_controller or not self.browser_controller.page:
            logger.error("Browser not available for link extraction.")
            return set()

        extracted_links = set()
        try:
            # Use Playwright's locator to find all anchor tags
            links = self.browser_controller.page.locator('a[href]').all()
            logger.debug(f"Found {len(links)} potential link elements on {current_url}.")

            for link_locator in links:
                try:
                    href = link_locator.get_attribute('href')
                    if href:
                        # Resolve relative URLs against the current page's URL
                        absolute_url = urljoin(current_url, href.strip())
                        normalized_url = self._normalize_url(absolute_url)

                        if normalized_url and self._is_valid_url(normalized_url):
                             # logger.debug(f"  Found link: {href} -> {normalized_url}")
                             extracted_links.add(normalized_url)
                        # else: logger.debug(f"  Skipping invalid/malformed link: {href} -> {normalized_url}")

                except Exception as link_err:
                    # Log error getting attribute but continue with others
                    logger.warning(f"Could not get href for a link element on {current_url}: {link_err}")
                    continue # Skip this link

        except Exception as e:
            logger.error(f"Error extracting links from {current_url}: {e}", exc_info=True)

        logger.info(f"Extracted {len(extracted_links)} unique, valid, normalized links from {current_url}.")
        return extracted_links

    def _get_test_step_suggestions(self,
                                   page_url: str,
                                   dom_context_str: Optional[str],
                                   screenshot_bytes: Optional[bytes]
                                   ) -> List[str]:
        """Asks the LLM to suggest specific test steps based on page URL, DOM, and screenshot."""
        logger.info(f"Requesting LLM suggestions for page: {page_url} (using DOM/Screenshot context)")

        prompt = f"""
        You are an AI Test Analyst identifying valuable test scenarios by suggesting specific test steps.
        The crawler is currently on the web page:
        URL: {page_url}

        Analyze the following page context:
        1.  **URL & Page Purpose:** Infer the primary purpose of this page (e.g., Login, Blog Post, Product Details, Form Submission, Search Results, Homepage).
        2.  **Visible DOM Elements:** Review the HTML snippet of visible elements. Note forms, primary action buttons (Submit, Add to Cart, Subscribe), key content areas, inputs, etc. Interactive elements are marked `[index]`, static with `(Static)`.
        3.  **Screenshot:** Analyze the visual layout, focusing on interactive elements and prominent information relevant to the page's purpose.

        **Visible DOM Context:**
        ```html
        {dom_context_str if dom_context_str else "DOM context not available."}
        ```
        {f"**Screenshot Analysis:** Please analyze the attached screenshot for layout, visible text, forms, and key interactive elements." if screenshot_bytes else "**Note:** No screenshot provided."}

        **Your Task:**
        Based on the inferred purpose and the page context (URL, DOM, Screenshot), suggest **one or two short sequences (totaling 4-7 steps)** of specific, actionable test steps representing **meaningful user interactions or verifications** related to the page's **core functionality**.

        **Step Description Requirements:**
        *   Each step should be a single, clear instruction (e.g., "Click 'Login' button", "Type 'test@example.com' into 'Email' field", "Verify 'Welcome Back!' message is displayed").
        *   Describe target elements clearly using visual labels, placeholders, or roles (e.g., 'Username field', 'Add to Cart button', 'Subscribe to newsletter checkbox'). **Do NOT include CSS selectors or indices `[index]`**.
        *   **Prioritize sequences:** Group related actions together logically (e.g., fill form fields -> click submit; select options -> add to cart).
        *   **Focus on core function:** Test the main reason the page exists (logging in, submitting data, viewing specific content details, adding an item, completing a search, signing up, etc.).
        *   **Include Verifications:** Crucially, add steps to verify expected outcomes after actions (e.g., "Verify success message 'Item Added' appears", "Verify error message 'Password required' is shown", "Verify user is redirected to dashboard page", "Verify shopping cart count increases").
        *   **AVOID:** Simply listing navigation links (header, footer, sidebar) unless they are part of a specific task *initiated* on this page (like password recovery). Avoid generic actions ("Click image", "Click text") without clear purpose or verification.

        **Examples of GOOD Step Sequences:**
        *   Login Page: `["Type 'testuser' into Username field", "Type 'wrongpass' into Password field", "Click Login button", "Verify 'Invalid credentials' error message is visible"]`
        *   Product Page: `["Select 'Red' from Color dropdown", "Click 'Add to Cart' button", "Verify cart icon shows '1 item'", "Navigate to the shopping cart page"]`
        *   Blog Page (if comments enabled): `["Scroll down to the comments section", "Type 'Great post!' into the comment input box", "Click the 'Submit Comment' button", "Verify 'Comment submitted successfully' message appears"]`
        *   Newsletter Signup Form: `["Enter 'John Doe' into the Full Name field", "Enter 'j.doe@email.com' into the Email field", "Click the 'Subscribe' button", "Verify confirmation text 'Thanks for subscribing!' is displayed"]`

        **Examples of BAD/LOW-VALUE Steps (to avoid):**
        *   `["Click Home link", "Click About Us link", "Click Contact link"]` (Just navigation, low value unless testing navigation itself specifically)
        *   `["Click the first image", "Click the second paragraph"]` (No clear purpose or verification)
        *   `["Type text into search bar"]` (Incomplete - what text? what next? add submit/verify)

        **Output Requirements:**
        - Provide a JSON object matching the required schema (`SuggestedTestStepsSchema`).
        - The `suggested_test_steps` list should contain 4-7 specific steps, ideally forming 1-2 meaningful sequences.
        - Provide brief `reasoning` explaining *why* these steps test the core function.

        Respond ONLY with the JSON object matching the schema.

        """

        # Call generate_json, passing image_bytes if available
        response_obj = self.llm_client.generate_json(
            SuggestedTestStepsSchema, # Use the updated schema class
            prompt,
            image_bytes=screenshot_bytes # Pass the image bytes here
        )

        if isinstance(response_obj, SuggestedTestStepsSchema):
            logger.debug(f"LLM suggested steps for {page_url}: {response_obj.suggested_test_steps} (Reason: {response_obj.reasoning})")
            # Validate the response list
            if response_obj.suggested_test_steps and isinstance(response_obj.suggested_test_steps, list):
                 valid_steps = [step for step in response_obj.suggested_test_steps if isinstance(step, str) and step.strip()]
                 if len(valid_steps) != len(response_obj.suggested_test_steps):
                      logger.warning(f"LLM response for {page_url} contained invalid step entries. Using only valid ones.")
                 return valid_steps
            else:
                 logger.warning(f"LLM did not return a valid list of steps for {page_url}.")
                 return []
        elif isinstance(response_obj, str): # Handle error string
            logger.error(f"LLM suggestion failed for {page_url}: {response_obj}")
            return []
        else: # Handle unexpected type
            logger.error(f"Unexpected response type from LLM for {page_url}: {type(response_obj)}")
            return []


    def crawl_and_suggest(self, start_url: str, max_pages: int = 10) -> Dict[str, Any]:
        """
        Starts the crawling process from the given URL.

        Args:
            start_url: The initial URL to start crawling from.
            max_pages: The maximum number of unique pages to visit and get suggestions for.

        Returns:
            A dictionary containing the results:
            {
                "success": bool,
                "message": str,
                "start_url": str,
                "base_domain": str,
                "pages_visited": int,
                "discovered_steps": Dict[str, List[str]] # {url: [flow1, flow2,...]}
            }
        """
        logger.info(f"Starting crawl from '{start_url}', max pages: {max_pages}")
        crawl_result = {
            "success": False,
            "message": "Crawl initiated.",
            "start_url": start_url,
            "base_domain": None,
            "pages_visited": 0,
            "discovered_steps": {}
        }

        # --- Initialization ---
        self.queue = []
        self.visited_urls = set()
        self.discovered_steps = {}

        normalized_start_url = self._normalize_url(start_url)
        if not normalized_start_url or not self._is_valid_url(normalized_start_url):
            crawl_result["message"] = f"Invalid start URL provided: {start_url}"
            logger.error(crawl_result["message"])
            return crawl_result

        self.base_domain = self._get_domain(normalized_start_url)
        if not self.base_domain:
             crawl_result["message"] = f"Could not extract domain from start URL: {start_url}"
             logger.error(crawl_result["message"])
             return crawl_result

        crawl_result["base_domain"] = self.base_domain
        self.queue.append(normalized_start_url)
        logger.info(f"Base domain set to: {self.base_domain}")

        try:
            # --- Setup Browser ---
            logger.info("Starting browser for crawler...")
            self.browser_controller = BrowserController(headless=self.headless)
            self.browser_controller.start()
            if not self.browser_controller.page:
                 raise RuntimeError("Failed to initialize browser page for crawler.")

            # --- Crawling Loop ---
            while self.queue and len(self.visited_urls) < max_pages:
                current_url = self.queue.pop(0)

                # Skip if already visited
                if current_url in self.visited_urls:
                    logger.debug(f"Skipping already visited URL: {current_url}")
                    continue

                # Check if it belongs to the target domain
                current_domain = self._get_domain(current_url)
                if current_domain != self.base_domain:
                    logger.debug(f"Skipping URL outside base domain ({self.base_domain}): {current_url}")
                    continue

                logger.info(f"Visiting ({len(self.visited_urls) + 1}/{max_pages}): {current_url}")
                self.visited_urls.add(current_url)
                crawl_result["pages_visited"] += 1

                dom_context_str: Optional[str] = None
                screenshot_bytes: Optional[bytes] = None

                # Navigate
                try:
                    self.browser_controller.goto(current_url)
                    # Optional: Add wait for load state if needed, goto has basic wait
                    self.browser_controller.page.wait_for_load_state('domcontentloaded', timeout=15000)
                    actual_url_after_nav = self.browser_controller.get_current_url() # Get the URL we actually landed on

                    # --- >>> ADDED DOMAIN CHECK AFTER NAVIGATION <<< ---
                    actual_domain = self._get_domain(actual_url_after_nav)
                    if actual_domain != self.base_domain:
                        logger.warning(f"Redirected outside base domain! "
                                       f"Initial: {current_url}, Final: {actual_url_after_nav} ({actual_domain}). Skipping further processing for this page.")
                        # Optional: Add the actual off-domain URL to visited to prevent loops if it links back?
                        off_domain_normalized = self._normalize_url(actual_url_after_nav)
                        if off_domain_normalized:
                            self.visited_urls.add(off_domain_normalized)
                        time.sleep(self.politeness_delay) # Still add delay
                        continue # Skip context gathering, link extraction, suggestions for this off-domain page
                    # --- Gather Context (DOM + Screenshot) ---
                    try:
                        logger.debug(f"Gathering DOM state for {current_url}...")
                        dom_state = self.browser_controller.get_structured_dom()
                        if dom_state and dom_state.element_tree:
                            dom_context_str, _ = dom_state.element_tree.generate_llm_context_string(context_purpose='verification') # Use verification context (more static elements)
                            logger.debug(f"DOM context string generated (length: {len(dom_context_str)}).")
                        else:
                             logger.warning(f"Could not get structured DOM for {current_url}.")
                             dom_context_str = "Error retrieving DOM structure."

                        logger.debug(f"Taking screenshot for {current_url}...")
                        screenshot_bytes = self.browser_controller.take_screenshot()
                        if not screenshot_bytes:
                             logger.warning(f"Failed to take screenshot for {current_url}.")

                    except Exception as context_err:
                        logger.error(f"Failed to gather context (DOM/Screenshot) for {current_url}: {context_err}")
                        dom_context_str = f"Error gathering context: {context_err}"
                        screenshot_bytes = None # Ensure screenshot is None if context gathering failed

                except Exception as nav_e:
                    logger.warning(f"Failed to navigate to {current_url}: {nav_e}. Skipping this page.")
                    # Don't add links or suggestions if navigation fails
                    continue # Skip to next URL in queue

                # Extract Links
                new_links = self._extract_links(current_url)
                for link in new_links:
                    if link not in self.visited_urls and self._get_domain(link) == self.base_domain:
                         if link not in self.queue: # Add only if not already queued
                              self.queue.append(link)

                # --- Get LLM Suggestions (using gathered context) ---
                suggestions = self._get_test_step_suggestions(
                    current_url,
                    dom_context_str,
                    screenshot_bytes
                )
                if suggestions:
                    self.discovered_steps[current_url] = suggestions 

                # Politeness delay
                logger.debug(f"Waiting {self.politeness_delay}s before next page...")
                time.sleep(self.politeness_delay)


            # --- Loop End ---
            crawl_result["success"] = True
            if len(self.visited_urls) >= max_pages:
                crawl_result["message"] = f"Crawl finished: Reached max pages limit ({max_pages})."
                logger.info(crawl_result["message"])
            elif not self.queue:
                crawl_result["message"] = f"Crawl finished: Explored all reachable pages within domain ({len(self.visited_urls)} visited)."
                logger.info(crawl_result["message"])
            else: # Should not happen unless error
                crawl_result["message"] = "Crawl finished unexpectedly."

            crawl_result["discovered_steps"] = self.discovered_steps


        except Exception as e:
            logger.critical(f"Critical error during crawl process: {e}", exc_info=True)
            crawl_result["message"] = f"Crawler failed with error: {e}"
            crawl_result["success"] = False
        finally:
            logger.info("--- Ending Crawl ---")
            if self.browser_controller:
                self.browser_controller.close()
                self.browser_controller = None

            logger.info(f"Crawl Summary: Visited {crawl_result['pages_visited']} pages. Found suggestions for {len(crawl_result.get('discovered_steps', {}))} pages.")

        return crawl_result
    
