# /dom/history/service.py
import hashlib
from typing import Optional, List, Dict # Added Dict

# Use relative imports
from ..views import DOMElementNode # Import from parent package's views
from .view import DOMHistoryElement, HashedDomElement # Import from sibling view

# Requires BrowserContext._enhanced_css_selector_for_element
# This needs to be available. Let's assume DomService provides it statically for now.
from ..service import DomService


class HistoryTreeProcessor:
    """
    Operations for comparing DOM elements across different states using hashing.
    """

    @staticmethod
    def convert_dom_element_to_history_element(dom_element: DOMElementNode) -> DOMHistoryElement:
        """Converts a live DOMElementNode to a serializable DOMHistoryElement."""
        if not dom_element: return None # Added safety check

        parent_branch_path = HistoryTreeProcessor._get_parent_branch_path(dom_element)
        # Use the static method from DomService to generate the selector
        css_selector = DomService._enhanced_css_selector_for_element(dom_element)

        # Ensure coordinate/viewport data is copied correctly
        page_coords = dom_element.page_coordinates.model_dump() if dom_element.page_coordinates else None
        viewport_coords = dom_element.viewport_coordinates.model_dump() if dom_element.viewport_coordinates else None
        viewport_info = dom_element.viewport_info.model_dump() if dom_element.viewport_info else None


        return DOMHistoryElement(
            tag_name=dom_element.tag_name,
            xpath=dom_element.xpath,
            highlight_index=dom_element.highlight_index,
            entire_parent_branch_path=parent_branch_path,
            attributes=dom_element.attributes,
            shadow_root=dom_element.shadow_root,
            css_selector=css_selector, # Use generated selector
            # Pass the Pydantic models directly if DOMHistoryElement expects them
            page_coordinates=dom_element.page_coordinates,
            viewport_coordinates=dom_element.viewport_coordinates,
            viewport_info=dom_element.viewport_info,
        )

    @staticmethod
    def find_history_element_in_tree(dom_history_element: DOMHistoryElement, tree: DOMElementNode) -> Optional[DOMElementNode]:
        """Finds an element in a new DOM tree that matches a historical element."""
        if not dom_history_element or not tree: return None

        hashed_dom_history_element = HistoryTreeProcessor._hash_dom_history_element(dom_history_element)

        # Define recursive search function
        def process_node(node: DOMElementNode) -> Optional[DOMElementNode]:
            if not isinstance(node, DOMElementNode): # Skip non-element nodes
                return None

            # Only hash and compare elements that could potentially match (e.g., have attributes/xpath)
            # Optimization: maybe check tag_name first?
            if node.tag_name == dom_history_element.tag_name:
                hashed_node = HistoryTreeProcessor._hash_dom_element(node)
                if hashed_node == hashed_dom_history_element:
                    # Found a match based on hash
                    # Optional: Add secondary checks here if needed (e.g., text snippet)
                    return node

            # Recursively search children
            for child in node.children:
                 # Important: Only recurse into DOMElementNode children
                 if isinstance(child, DOMElementNode):
                      result = process_node(child)
                      if result is not None:
                           return result # Return immediately if found in subtree

            return None # Not found in this branch

        return process_node(tree)

    @staticmethod
    def compare_history_element_and_dom_element(dom_history_element: DOMHistoryElement, dom_element: DOMElementNode) -> bool:
        """Compares a historical element and a live element using hashes."""
        if not dom_history_element or not dom_element: return False

        hashed_dom_history_element = HistoryTreeProcessor._hash_dom_history_element(dom_history_element)
        hashed_dom_element = HistoryTreeProcessor._hash_dom_element(dom_element)

        return hashed_dom_history_element == hashed_dom_element

    @staticmethod
    def _hash_dom_history_element(dom_history_element: DOMHistoryElement) -> Optional[HashedDomElement]:
        """Generates a hash object from a DOMHistoryElement."""
        if not dom_history_element: return None

        # Use the stored parent path
        branch_path_hash = HistoryTreeProcessor._parent_branch_path_hash(dom_history_element.entire_parent_branch_path)
        attributes_hash = HistoryTreeProcessor._attributes_hash(dom_history_element.attributes)
        xpath_hash = HistoryTreeProcessor._xpath_hash(dom_history_element.xpath)

        return HashedDomElement(branch_path_hash, attributes_hash, xpath_hash)

    @staticmethod
    def _hash_dom_element(dom_element: DOMElementNode) -> Optional[HashedDomElement]:
        """Generates a hash object from a live DOMElementNode."""
        if not dom_element: return None

        parent_branch_path = HistoryTreeProcessor._get_parent_branch_path(dom_element)
        branch_path_hash = HistoryTreeProcessor._parent_branch_path_hash(parent_branch_path)
        attributes_hash = HistoryTreeProcessor._attributes_hash(dom_element.attributes)
        xpath_hash = HistoryTreeProcessor._xpath_hash(dom_element.xpath)
        # text_hash = DomTreeProcessor._text_hash(dom_element) # Text hash still excluded

        return HashedDomElement(branch_path_hash, attributes_hash, xpath_hash)

    @staticmethod
    def _get_parent_branch_path(dom_element: DOMElementNode) -> List[str]:
        """Gets the list of tag names from the element up to the root."""
        parents: List[str] = [] # Store tag names directly
        current_element: Optional[DOMElementNode] = dom_element
        while current_element is not None:
            # Prepend tag name to maintain order from root to element
            parents.insert(0, current_element.tag_name)
            current_element = current_element.parent # Access the parent attribute

        # The loop includes the element itself, the definition might imply *excluding* it
        # If path should *exclude* the element itself, remove the first element:
        # if parents: parents.pop(0) # No, the JS build tree Xpath includes self, let's keep it consistent
        return parents

    @staticmethod
    def _parent_branch_path_hash(parent_branch_path: List[str]) -> str:
        """Hashes the parent branch path string."""
        # Normalize: use lowercase tags and join consistently
        parent_branch_path_string = '/'.join(tag.lower() for tag in parent_branch_path)
        return hashlib.sha256(parent_branch_path_string.encode('utf-8')).hexdigest()

    @staticmethod
    def _attributes_hash(attributes: Dict[str, str]) -> str:
        """Hashes the element's attributes dictionary."""
        # Ensure consistent order by sorting keys
        # Normalize attribute values (e.g., strip whitespace?) - Keep simple for now
        attributes_string = ''.join(f'{key}={attributes[key]}' for key in sorted(attributes.keys()))
        return hashlib.sha256(attributes_string.encode('utf-8')).hexdigest()

    @staticmethod
    def _xpath_hash(xpath: str) -> str:
        """Hashes the element's XPath."""
        # Normalize XPath? (e.g., lowercase tags) - Assume input is consistent for now
        return hashlib.sha256(xpath.encode('utf-8')).hexdigest()

    # _text_hash remains commented out / unused based on the original code's decision
    # @staticmethod
    # def _text_hash(dom_element: DOMElementNode) -> str:
    #     """ """
    #     text_string = dom_element.get_all_text_till_next_clickable_element()
    #     return hashlib.sha256(text_string.encode()).hexdigest()

