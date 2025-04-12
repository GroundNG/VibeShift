# dom/views.py 
from dataclasses import dataclass, field, KW_ONLY # Use field for default_factory
from functools import cached_property
from typing import TYPE_CHECKING, Dict, List, Optional, Union # Union added
import re # Added for selector generation

# Use relative imports if within the same package structure
from .history.view import CoordinateSet, HashedDomElement, ViewportInfo # Adjusted import

# Placeholder decorator if not using utils.time_execution_sync
def time_execution_sync(label):
    def decorator(func):
        def wrapper(*args, **kwargs):
            # Basic logging
            # logger.debug(f"Executing {label}...")
            result = func(*args, **kwargs)
            # logger.debug(f"Finished {label}.")
            return result
        return wrapper
    return decorator

# Avoid circular import issues
if TYPE_CHECKING:
    # This creates a forward reference issue if DOMElementNode itself is in this file.
    # We need to define DOMElementNode before DOMBaseNode if DOMBaseNode references it.
    # Let's adjust the structure slightly or use string hints.
    pass # Forward reference handled by structure/string hints below

@dataclass(frozen=False)
class DOMBaseNode:
    # Parent needs to be Optional and potentially use string hint if defined later
    parent: Optional['DOMElementNode'] = None # Default to None
    is_visible: bool = False # Provide default

@dataclass(frozen=False)
class DOMTextNode(DOMBaseNode):
     # --- Field ordering within subclass matters less with KW_ONLY ---
    # --- but arguments after the marker MUST be passed by keyword ---
    _ : KW_ONLY # <--- Add KW_ONLY marker

    # Fields defined in this class (now keyword-only)
    text: str
    type: str = 'TEXT_NODE'

    def has_parent_with_highlight_index(self) -> bool:
        current = self.parent
        while current is not None:
            if current.highlight_index is not None:
                return True
            current = current.parent
        return False

    # These visibility checks might be less useful now that JS handles it, but keep for now
    def is_parent_in_viewport(self) -> bool:
        if self.parent is None:
            return False
        return self.parent.is_in_viewport

    def is_parent_top_element(self) -> bool:
        if self.parent is None:
            return False
        return self.parent.is_top_element

# Define DOMElementNode *before* DOMBaseNode references it fully, or ensure Optional['DOMElementNode'] works
@dataclass(frozen=False)
class DOMElementNode(DOMBaseNode):
    """
    Represents an element node in the processed DOM tree.
    Includes information about interactivity, visibility, and structure.
    """
    tag_name: str = ""
    xpath: str = ""
    attributes: Dict[str, str] = field(default_factory=dict)
    # Use Union with string hint for forward reference if needed, or ensure DOMTextNode is defined first
    children: List[Union['DOMElementNode', DOMTextNode]] = field(default_factory=list)
    is_interactive: bool = False
    is_top_element: bool = False
    is_in_viewport: bool = False
    shadow_root: bool = False
    highlight_index: Optional[int] = None
    page_coordinates: Optional[CoordinateSet] = None
    viewport_coordinates: Optional[CoordinateSet] = None
    viewport_info: Optional[ViewportInfo] = None
    css_selector: Optional[str] = None # Added field for robust selector

    def __repr__(self) -> str:
        # ... (repr logic remains the same) ...
        tag_str = f'<{self.tag_name}'
        for key, value in self.attributes.items():
             # Shorten long values in repr
             value_repr = value if len(value) < 50 else value[:47] + '...'
             tag_str += f' {key}="{value_repr}"'
        tag_str += '>'

        extras = []
        if self.is_interactive: extras.append('interactive')
        if self.is_top_element: extras.append('top')
        if self.is_in_viewport: extras.append('in-viewport')
        if self.shadow_root: extras.append('shadow-root')
        if self.highlight_index is not None: extras.append(f'highlight:{self.highlight_index}')
        if self.css_selector: extras.append(f'css:"{self.css_selector[:50]}..."') # Show generated selector

        if extras:
            tag_str += f' [{", ".join(extras)}]'
        return tag_str

    @cached_property
    def hash(self) -> HashedDomElement:
        """ Lazily computes and caches the hash of the element using HistoryTreeProcessor. """
        # Use relative import within the method to avoid top-level circular dependencies
        from .history.service import HistoryTreeProcessor
        # Ensure HistoryTreeProcessor._hash_dom_element exists and is static or accessible
        return HistoryTreeProcessor._hash_dom_element(self)

    def get_all_text_till_next_clickable_element(self, max_depth: int = -1) -> str:
        """
        Recursively collects all text content within this element, stopping descent
        if a nested interactive element (with a highlight_index) is encountered.
        """
        text_parts = []

        def collect_text(node: Union['DOMElementNode', DOMTextNode], current_depth: int) -> None:
            if max_depth != -1 and current_depth > max_depth:
                return

            # Check if the node itself is interactive and not the starting node
            if isinstance(node, DOMElementNode) and node is not self and node.highlight_index is not None:
                # Stop recursion down this path if we hit an interactive element
                return

            if isinstance(node, DOMTextNode):
                # Only include visible text nodes
                if node.is_visible:
                    text_parts.append(node.text)
            elif isinstance(node, DOMElementNode):
                # Recursively process children
                for child in node.children:
                    collect_text(child, current_depth + 1)

        # Start collection from the element itself
        collect_text(self, 0)
        # Join collected parts and clean up whitespace
        return '\n'.join(filter(None, (tp.strip() for tp in text_parts))).strip()


    @time_execution_sync('--clickable_elements_to_string')
    def clickable_elements_to_string(self, include_attributes: Optional[List[str]] = None) -> str:
        """
        Generates a string representation of the interactive elements tree,
        suitable for LLM context. Uses highlight indices.
        """
        formatted_lines = []

        def process_node(node: Union['DOMElementNode', DOMTextNode], depth: int) -> None:
            indent = '  ' * depth
            if isinstance(node, DOMElementNode):
                # Process and print the element if it has a highlight_index (meaning it's interactive)
                if node.highlight_index is not None:
                    # Extract relevant attributes to display
                    attributes_to_show = {}
                    if include_attributes: # If specific attributes requested
                        for attr_key in include_attributes:
                            if attr_key in node.attributes:
                                attributes_to_show[attr_key] = node.attributes[attr_key]
                    else: # Default attributes to show (can be adjusted)
                         default_attrs = ['id', 'name', 'class', 'aria-label', 'placeholder', 'role', 'type', 'value', 'title']
                         for attr_key in default_attrs:
                              if attr_key in node.attributes and node.attributes[attr_key]: # Only show if value exists
                                   attributes_to_show[attr_key] = node.attributes[attr_key]


                    attrs_str = ""
                    if attributes_to_show:
                         parts = []
                         for key, value in attributes_to_show.items():
                              # Abbreviate long values in the output string
                              display_value = value if len(value) < 50 else value[:47] + '...'
                              # Escape quotes in value if necessary
                              display_value = display_value.replace('"', '&quot;')
                              parts.append(f'{key}="{display_value}"')
                         attrs_str = " ".join(parts)

                    # Get text content associated with this interactive element
                    text_content = node.get_all_text_till_next_clickable_element()
                    # Clean text content (remove extra newlines/spaces)
                    text_content = ' '.join(text_content.split()) if text_content else ""


                    line = f"{indent}[{node.highlight_index}]<{node.tag_name}"
                    if attrs_str:
                        line += f" {attrs_str}"
                    if text_content:
                        line += f">{text_content}</{node.tag_name}>"
                    else:
                        line += " />" # Self-closing style if no text

                    formatted_lines.append(line)

                # Always process children, regardless of whether the parent was highlighted
                for child in node.children:
                    process_node(child, depth + 1 if node.highlight_index is None else depth) # Keep indent same if parent printed

            # Text nodes are handled by get_all_text_till_next_clickable_element,
            # so we don't explicitly process them here for the summary string.

        # Start processing from the root element provided
        process_node(self, 0)
        return '\n'.join(formatted_lines)

    # get_file_upload_element can remain the same

    def get_file_upload_element(self, check_siblings: bool = True) -> Optional['DOMElementNode']:
        # Check if current element is a file input
        if self.tag_name == 'input' and self.attributes.get('type') == 'file':
            return self

        # Check children
        for child in self.children:
            if isinstance(child, DOMElementNode):
                result = child.get_file_upload_element(check_siblings=False)
                if result:
                    return result

        # Check siblings only for the initial call
        if check_siblings and self.parent:
            for sibling in self.parent.children:
                if sibling is not self and isinstance(sibling, DOMElementNode):
                    result = sibling.get_file_upload_element(check_siblings=False)
                    if result:
                        return result

        return None

# Type alias for the selector map
SelectorMap = Dict[int, DOMElementNode]


@dataclass
class DOMState:
    """Holds the state of the processed DOM at a point in time."""
    element_tree: DOMElementNode
    selector_map: SelectorMap