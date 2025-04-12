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
        Generates a more robust CSS selector for a given DOMElementNode.
        Attempts to use unique attributes first, then falls back to tag/class/nth-child.
        """
        if not isinstance(element, DOMElementNode):
            return ''

        # 1. Prioritize unique IDs
        if 'id' in element.attributes and element.attributes['id']:
            # Basic ID validation (doesn't start with number, no weird chars)
            element_id = element.attributes['id']
            if re.match(r'^[A-Za-z_][\w\-]*$', element_id):
                 # Check uniqueness within its parent context if possible (hard without page access here)
                 # Assume unique for now if it looks like a valid ID
                 selector = f"#{element_id}"
                 # Quick check in attributes string representation for common non-unique patterns
                 if ' ' not in element_id and ':' not in element_id: # Avoid complex or likely dynamic IDs
                      logger.debug(f"Using ID selector for {element.tag_name}: {selector}")
                      return selector

        # 2. Try data-testid or other stable test attributes
        for test_attr in ['data-testid', 'data-test-id', 'data-cy']:
            if test_attr in element.attributes and element.attributes[test_attr]:
                val = element.attributes[test_attr]
                selector = f"[{test_attr}='{val}']"
                logger.debug(f"Using test attribute selector for {element.tag_name}: {selector}")
                return selector

        # 3. Use name attribute, common for forms
        if 'name' in element.attributes and element.attributes['name']:
             selector = f"{element.tag_name}[name='{element.attributes['name']}']"
             logger.debug(f"Using name attribute selector for {element.tag_name}: {selector}")
             return selector

        # 4. Fallback to tag name, classes, and nth-child selector (less robust)
        selector = element.tag_name

        if 'class' in element.attributes and element.attributes['class']:
            classes = element.attributes['class'].split()
            stable_classes = [c for c in classes if not re.match(r'.*[\d:]', c)] # Filter out potentially dynamic classes
            if stable_classes:
                selector += '.' + '.'.join(stable_classes)

        # Calculate nth-child/nth-of-type relative to parent
        if element.parent:
            siblings = [
                sibling for sibling in element.parent.children
                if isinstance(sibling, DOMElementNode) and sibling.tag_name == element.tag_name
            ]
            try:
                index = siblings.index(element) + 1
                selector += f':nth-of-type({index})'
            except ValueError:
                 pass # Element not found among siblings of same type? Shouldn't happen.

        # Try adding parent context to disambiguate further (limited recursion)
        if element.parent and element.parent.tag_name != 'body':
            parent_selector = DomService._enhanced_css_selector_for_element(element.parent)
            # Avoid overly complex selectors by limiting parent context addition
            if parent_selector and len(parent_selector.split('>')) < 3: # Limit depth
                 selector = f"{parent_selector} > {selector}"

        logger.debug(f"Using fallback selector for {element.tag_name}: {selector}")
        return selector