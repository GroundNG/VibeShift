# dom/service.py
import gc
import json
import logging
from dataclasses import dataclass
from importlib import resources # Use importlib.resources
from typing import TYPE_CHECKING, Optional, Tuple, Dict, List
import re

# Use relative imports if within the same package structure
from .views import (
    DOMBaseNode,
    DOMElementNode,
    DOMState,
    DOMTextNode,
    SelectorMap,
    ViewportInfo, # Added ViewportInfo here
    CoordinateSet # Added CoordinateSet
)
# Removed utils import assuming time_execution_async is defined elsewhere or removed for brevity
# from ..utils import time_execution_async # Example relative import if utils is one level up

if TYPE_CHECKING:
    from playwright.sync_api import Page # Use sync_api for this repo

logger = logging.getLogger(__name__)

# Decorator placeholder if not using utils.time_execution_async
def time_execution_async(label):
    def decorator(func):
        # In a sync context, this decorator needs adjustment or removal
        # For simplicity here, we'll just make it pass through in the sync version
        def wrapper(*args, **kwargs):
            # logger.debug(f"Executing {label}...") # Basic logging
            result = func(*args, **kwargs)
            # logger.debug(f"Finished {label}.") # Basic logging
            return result
        return wrapper
    return decorator


class DomService:
    def __init__(self, page: 'Page'):
        self.page = page
        self.xpath_cache = {} # Consider if this cache is still needed/used effectively

        # Correctly load JS using importlib.resources relative to this file
        try:
            # Assuming buildDomTree.js is in the same directory 'dom'
            with resources.path(__package__, 'buildDomTree.js') as js_path:
                 self.js_code = js_path.read_text(encoding='utf-8')
            logger.debug("buildDomTree.js loaded successfully.")
        except FileNotFoundError:
             logger.error("buildDomTree.js not found in the 'dom' package directory!")
             raise
        except Exception as e:
             logger.error(f"Error loading buildDomTree.js: {e}", exc_info=True)
             raise

    # region - Clickable elements
    @time_execution_async('--get_clickable_elements')
    def get_clickable_elements(
        self,
        highlight_elements: bool = True,
        focus_element: int = -1,
        viewport_expansion: int = 0,
    ) -> DOMState:
        """Gets interactive elements and DOM structure. Sync version."""
        logger.debug(f"Calling _build_dom_tree with highlight={highlight_elements}, focus={focus_element}, expansion={viewport_expansion}")
        # In sync context, _build_dom_tree should be sync
        element_tree, selector_map = self._build_dom_tree(highlight_elements, focus_element, viewport_expansion)
        return DOMState(element_tree=element_tree, selector_map=selector_map)

    # Removed get_cross_origin_iframes for brevity, can be added back if needed

    # @time_execution_async('--build_dom_tree') # Adjust decorator if needed for sync
    def _build_dom_tree(
        self,
        highlight_elements: bool,
        focus_element: int,
        viewport_expansion: int,
    ) -> Tuple[DOMElementNode, SelectorMap]:
        """Builds the DOM tree by executing JS in the browser. Sync version."""
        logger.debug("Executing _build_dom_tree...")
        if self.page.evaluate('1+1') != 2:
            raise ValueError('The page cannot evaluate javascript code properly')

        if self.page.url == 'about:blank' or self.page.url == '':
            logger.info("Page URL is blank, returning empty DOM structure.")
            # short-circuit if the page is a new empty tab for speed
            return (
                DOMElementNode(
                    tag_name='body',
                    xpath='',
                    attributes={},
                    children=[],
                    is_visible=False,
                    parent=None,
                ),
                {},
            )

        debug_mode = logger.getEffectiveLevel() <= logging.DEBUG
        args = {
            'doHighlightElements': highlight_elements,
            'focusHighlightIndex': focus_element,
            'viewportExpansion': viewport_expansion,
            'debugMode': debug_mode,
        }
        logger.debug(f"Evaluating buildDomTree.js with args: {args}")

        try:
            # Use evaluate() directly in sync context
            eval_page: dict = self.page.evaluate(f"({self.js_code})", args)

        except Exception as e:
            logger.error(f"Error evaluating buildDomTree.js: {type(e).__name__}: {e}", exc_info=False) # Less verbose logging
            logger.debug(f"JS Code Snippet (first 500 chars):\n{self.js_code[:500]}...") # Log JS snippet on error
            # Try to get page state for context
            try:
                 page_url = self.page.url
                 page_title = self.page.title()
                 logger.error(f"Error occurred on page: URL='{page_url}', Title='{page_title}'")
            except Exception as page_state_e:
                 logger.error(f"Could not get page state after JS error: {page_state_e}")
            raise RuntimeError(f"Failed to evaluate DOM building script: {e}") from e # Re-raise a standard error


        # Only log performance metrics in debug mode
        if debug_mode and 'perfMetrics' in eval_page:
             logger.debug(
                 'DOM Tree Building Performance Metrics for: %s\n%s',
                 self.page.url,
                 json.dumps(eval_page['perfMetrics'], indent=2),
             )

        if 'map' not in eval_page or 'rootId' not in eval_page:
            logger.error(f"Invalid structure returned from buildDomTree.js: Missing 'map' or 'rootId'. Response keys: {eval_page.keys()}")
            # Log more details if possible
            logger.error(f"JS Eval Response Snippet: {str(eval_page)[:1000]}...")
            # Return empty structure to prevent downstream errors
            return (DOMElementNode(tag_name='body', xpath='', attributes={}, children=[], is_visible=False, parent=None), {})
            # raise ValueError("Invalid structure returned from DOM building script.")

        # Use sync _construct_dom_tree
        return self._construct_dom_tree(eval_page)

    # @time_execution_async('--construct_dom_tree') # Adjust decorator if needed for sync
    def _construct_dom_tree(
        self,
        eval_page: dict,
    ) -> Tuple[DOMElementNode, SelectorMap]:
        """Constructs the Python DOM tree from the JS map. Sync version."""
        logger.debug("Constructing Python DOM tree from JS map...")
        js_node_map = eval_page['map']
        js_root_id = eval_page.get('rootId') # Use .get for safety

        if js_root_id is None:
             logger.error("JS evaluation result missing 'rootId'. Cannot build tree.")
             # Return empty structure
             return (DOMElementNode(tag_name='body', xpath='', attributes={}, children=[], is_visible=False, parent=None), {})


        selector_map: SelectorMap = {}
        node_map: Dict[str, DOMBaseNode] = {} # Use string keys consistently

        # Iterate through the JS map provided by the browser script
        for id_str, node_data in js_node_map.items():
            if not isinstance(node_data, dict):
                 logger.warning(f"Skipping invalid node data (not a dict) for ID: {id_str}")
                 continue

            node, children_ids_str = self._parse_node(node_data)
            if node is None:
                continue # Skip nodes that couldn't be parsed

            node_map[id_str] = node # Store with string ID

            # If the node is an element node with a highlight index, add it to the selector map
            if isinstance(node, DOMElementNode) and node.highlight_index is not None:
                selector_map[node.highlight_index] = node

            # Link children to this node if it's an element node
            if isinstance(node, DOMElementNode):
                for child_id_str in children_ids_str:
                    child_node = node_map.get(child_id_str) # Use .get() for safety
                    if child_node:
                        # Set the parent reference on the child node
                        child_node.parent = node
                        # Add the child node to the current node's children list
                        node.children.append(child_node)
                    else:
                        # This can happen if a child node was invalid or filtered out
                        logger.debug(f"Child node with ID '{child_id_str}' not found in node_map while processing parent '{id_str}'.")


        # Retrieve the root node using the root ID from the evaluation result
        root_node = node_map.get(str(js_root_id))

        # Clean up large intermediate structures
        del node_map
        del js_node_map
        gc.collect()

        # Validate the root node
        if root_node is None or not isinstance(root_node, DOMElementNode):
            logger.error(f"Failed to find valid root DOMElementNode with ID '{js_root_id}'.")
            # Return a default empty body node to avoid crashes
            return (DOMElementNode(tag_name='body', xpath='', attributes={}, children=[], is_visible=False, parent=None), selector_map)

        logger.debug("Finished constructing Python DOM tree.")
        return root_node, selector_map


    def _parse_node(
        self,
        node_data: dict,
    ) -> Tuple[Optional[DOMBaseNode], List[str]]: # Return string IDs
        """Parses a single node dictionary from JS into a Python DOM object. Sync version."""
        if not node_data:
            return None, []

        node_type = node_data.get('type') # Check if it's explicitly a text node

        if node_type == 'TEXT_NODE':
             # Handle Text Nodes
             text = node_data.get('text', '')
             if not text: # Skip empty text nodes early
                  return None, []
             text_node = DOMTextNode(
                 text=text,
                 is_visible=node_data.get('isVisible', False), # Use .get for safety
                 parent=None, # Parent set later during construction
             )
             return text_node, []
        elif 'tagName' in node_data:
             # Handle Element Nodes
             tag_name = node_data['tagName']

             # Process coordinates if they exist (using Pydantic models from view)
             page_coords_data = node_data.get('pageCoordinates')
             viewport_coords_data = node_data.get('viewportCoordinates')
             viewport_info_data = node_data.get('viewportInfo')

             page_coordinates = CoordinateSet(**page_coords_data) if page_coords_data else None
             viewport_coordinates = CoordinateSet(**viewport_coords_data) if viewport_coords_data else None
             viewport_info = ViewportInfo(**viewport_info_data) if viewport_info_data else None

             element_node = DOMElementNode(
                 tag_name=tag_name.lower(), # Ensure lowercase
                 xpath=node_data.get('xpath', ''),
                 attributes=node_data.get('attributes', {}),
                 children=[], # Children added later
                 is_visible=node_data.get('isVisible', False),
                 is_interactive=node_data.get('isInteractive', False),
                 is_top_element=node_data.get('isTopElement', False),
                 is_in_viewport=node_data.get('isInViewport', False),
                 highlight_index=node_data.get('highlightIndex'), # Can be None
                 shadow_root=node_data.get('shadowRoot', False),
                 parent=None, # Parent set later
                 # Add coordinate fields
                 page_coordinates=page_coordinates,
                 viewport_coordinates=viewport_coordinates,
                 viewport_info=viewport_info,
                 # Enhanced CSS selector added later if needed
                 css_selector=None,
             )
             # Children IDs are strings from the JS map
             children_ids_str = node_data.get('children', [])
             # Basic validation
             if not isinstance(children_ids_str, list):
                 logger.warning(f"Invalid children format for node {node_data.get('xpath')}, expected list, got {type(children_ids_str)}. Treating as empty.")
                 children_ids_str = []

             return element_node, [str(cid) for cid in children_ids_str] # Ensure IDs are strings
        else:
             # Skip nodes that are neither TEXT_NODE nor have a tagName (e.g., comments processed out by JS)
             logger.debug(f"Skipping node data without 'type' or 'tagName': {str(node_data)[:100]}...")
             return None, []

    # Add the helper to generate enhanced CSS selectors (adapted from BrowserContext)
    # This could also live in a dedicated selector utility class/module
    @staticmethod
    def _enhanced_css_selector_for_element(element: DOMElementNode) -> str:
        """
        Generates a more robust CSS selector, prioritizing stable attributes.
        RECORDER FOCUS: Prioritize ID, data-testid, name, stable classes. Fallback carefully.
        """
        if not isinstance(element, DOMElementNode):
            return ''

        # Escape CSS identifiers (simple version, consider edge cases)
        def escape_css(value):
            if not value: return ''
            # Basic escape for characters that are problematic in unquoted identifiers/strings
            # See: https://developer.mozilla.org/en-US/docs/Web/CSS/string#escaping_characters
            # This is NOT exhaustive but covers common cases.
            return re.sub(r'([!"#$%&\'()*+,./:;<=>?@\[\\\]^`{|}~])', r'\\\1', value)


        # --- Attribute Priority Order ---
        # 1. ID (if reasonably unique-looking)
        if 'id' in element.attributes and element.attributes['id']:
            element_id = element.attributes['id'].strip()
            if element_id and not element_id.isdigit() and ' ' not in element_id and ':' not in element_id:
                 escaped_id = escape_css(element_id)
                 selector = f"#{escaped_id}"
                 # If ID seems generic, add tag name
                 if len(element_id) < 6 and element.tag_name not in ['div', 'span']: # Don't add for generic containers unless ID is short
                     return f"{element.tag_name}{selector}"
                 return selector

        # 2. Stable Data Attributes
        for test_attr in ['data-testid', 'data-test-id', 'data-cy', 'data-qa']:
            if test_attr in element.attributes and element.attributes[test_attr]:
                val = element.attributes[test_attr].strip()
                if val:
                     escaped_val = escape_css(val)
                     selector = f"[{test_attr}='{escaped_val}']"
                     # Add tag name if value seems generic
                     if len(val) < 5:
                         return f"{element.tag_name}{selector}"
                     return selector

        # 3. Name Attribute
        if 'name' in element.attributes and element.attributes['name']:
             name_val = element.attributes['name'].strip()
             if name_val:
                  escaped_name = escape_css(name_val)
                  selector = f"{element.tag_name}[name='{escaped_name}']"
                  return selector

        # 4. Aria-label
        if 'aria-label' in element.attributes and element.attributes['aria-label']:
             aria_label = element.attributes['aria-label'].strip()
             # Ensure label is reasonably specific (not just whitespace or very short)
             if aria_label and len(aria_label) > 2 and len(aria_label) < 80:
                  escaped_label = escape_css(aria_label)
                  selector = f"{element.tag_name}[aria-label='{escaped_label}']"
                  return selector

        # 5. Placeholder (for inputs)
        if element.tag_name == 'input' and 'placeholder' in element.attributes and element.attributes['placeholder']:
             placeholder = element.attributes['placeholder'].strip()
             if placeholder:
                  escaped_placeholder = escape_css(placeholder)
                  selector = f"input[placeholder='{escaped_placeholder}']"
                  return selector

        # --- Text Content Strategy (Use cautiously) ---
        # Get DIRECT, visible text content of the element itself
        direct_text = ""
        if element.is_visible: # Only consider text if element is visible
            texts = []
            for child in element.children:
                if isinstance(child, DOMTextNode) and child.is_visible:
                    texts.append(child.text.strip())
            direct_text = ' '.join(filter(None, texts)).strip()

        # 6. Specific Text Content (if short, unique-looking, and element type is suitable)
        suitable_text_tags = {'button', 'a', 'span', 'label', 'legend', 'h1', 'h2', 'h3', 'h4', 'p', 'li', 'td', 'th', 'dt', 'dd'}
        if direct_text and element.tag_name in suitable_text_tags and 2 < len(direct_text) < 60: # Avoid overly long or short text
             # Basic check for uniqueness (could be improved by checking siblings)
             # Check if it looks like dynamic content (e.g., numbers only, dates) - skip if so
             if not direct_text.isdigit() and not re.match(r'^\$?[\d,.]+$', direct_text): # Avoid pure numbers/prices
                # Use Playwright's text selector (escapes internally)
                # Note: This requires Playwright >= 1.15 or so for :text pseudo-class
                # Using :has-text is generally safer as it looks within descendants too,
                # but here we specifically want the *direct* text match.
                # Let's try combining tag and text for specificity.
                # Playwright handles quotes inside the text automatically.
                selector = f"{element.tag_name}:text-is('{direct_text}')"
                # Alternative: :text() - might be less strict about whitespace
                # selector = f"{element.tag_name}:text('{direct_text}')"
                # Let's try to validate this selector immediately if possible (costly)
                # For now, return it optimistically.
                return selector

        # --- Fallbacks (Structure and Class) ---
        base_selector = element.tag_name
        stable_classes_used = []

        # 7. Stable Class Names (Filter more strictly)
        if 'class' in element.attributes and element.attributes['class']:
            classes = element.attributes['class'].strip().split()
            stable_classes = [
                c for c in classes
                if c and not c.isdigit() and
                   not re.search(r'\d', c) and # No digits at all
                   not re.match(r'.*(--|__|is-|has-|js-|active|selected|disabled|hidden).*', c, re.IGNORECASE) and # Avoid common states/modifiers/js
                   not re.match(r'^[a-zA-Z]{1,2}$', c) and # Avoid 1-2 letter classes (often layout helpers)
                   len(c) > 2 and len(c) < 30 # Reasonable length
            ]
            if stable_classes:
                stable_classes.sort()
                stable_classes_used = stable_classes # Store for nth-of-type check
                base_selector += '.' + '.'.join(escape_css(c) for c in stable_classes)

        # --- Ancestor Context (Find nearest stable ancestor) ---
        # Try to find a parent with ID or data-testid to anchor the selector
        stable_ancestor_selector = None
        current = element.parent
        depth = 0
        max_depth = 4 # How far up to look for an anchor
        while current and depth < max_depth:
            ancestor_selector_part = None
            if 'id' in current.attributes and current.attributes['id']:
                 ancestor_id = current.attributes['id'].strip()
                 if ancestor_id and not ancestor_id.isdigit() and ' ' not in ancestor_id:
                      ancestor_selector_part = f"#{escape_css(ancestor_id)}"
            elif not ancestor_selector_part: # Check testid only if ID not found
                for test_attr in ['data-testid', 'data-test-id']:
                    if test_attr in current.attributes and current.attributes[test_attr]:
                        val = current.attributes[test_attr].strip()
                        if val:
                            ancestor_selector_part = f"[{test_attr}='{escape_css(val)}']"
                            break # Found one
            # If we found a stable part for the ancestor, use it
            if ancestor_selector_part:
                stable_ancestor_selector = ancestor_selector_part
                break # Stop searching up
            current = current.parent
            depth += 1

        # Combine ancestor and base selector if ancestor found
        final_selector = f"{stable_ancestor_selector} >> {base_selector}" if stable_ancestor_selector else base_selector

        # 8. Add :nth-of-type ONLY if multiple siblings match the current selector AND no unique attribute/text was found
        # This check becomes more complex with the ancestor path. We simplify here.
        # Only add nth-of-type if we didn't find a unique ID/testid/name/text for the element itself.
        needs_disambiguation = (stable_ancestor_selector is None) and \
                               (base_selector == element.tag_name or base_selector.startswith(element.tag_name + '.')) # Only tag or tag+class

        if needs_disambiguation and element.parent:
            try:
                # Find siblings matching the base selector part (tag + potentially classes)
                matching_siblings = []
                for sib in element.parent.children:
                    if isinstance(sib, DOMElementNode) and sib.tag_name == element.tag_name:
                        # Check classes if they were used in the base selector
                        if stable_classes_used:
                            if DomService._check_classes_match(sib, stable_classes_used):
                                matching_siblings.append(sib)
                        else: # No classes used, just match tag
                            matching_siblings.append(sib)

                if len(matching_siblings) > 1:
                     try:
                          index = matching_siblings.index(element) + 1
                          final_selector += f':nth-of-type({index})'
                     except ValueError:
                          logger.warning(f"Element not found in its own filtered sibling list for nth-of-type. Selector: {final_selector}")
            except Exception as e:
                logger.warning(f"Error during nth-of-type calculation: {e}. Selector: {final_selector}")

        # 9. FINAL FALLBACK: Use original XPath if selector is still not specific
        if final_selector == element.tag_name and element.xpath:
             logger.warning(f"Selector for {element.tag_name} is just the tag. Falling back to XPath: {element.xpath}")
             # Returning XPath directly might cause issues if executor expects CSS.
             # Playwright can handle css=<xpath>, so let's return that.
             return f"css={element.xpath}"

        return final_selector
    
    @staticmethod
    def _check_classes_match(element: DOMElementNode, required_classes: List[str]) -> bool:
        """Helper to check if an element has all the required classes."""
        if 'class' not in element.attributes or not element.attributes['class']:
            return False
        element_classes = set(element.attributes['class'].strip().split())
        return all(req_class in element_classes for req_class in required_classes)