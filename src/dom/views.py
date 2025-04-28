# /src/dom/views.py 
from dataclasses import dataclass, field, KW_ONLY # Use field for default_factory
from functools import cached_property
from typing import TYPE_CHECKING, Dict, List, Optional, Union, Literal, Tuple
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
    def generate_llm_context_string(self, 
            include_attributes: Optional[List[str]] = None, 
            max_static_elements_action: int = 50, # Max static elements for action context
            max_static_elements_verification: int = 150, # Allow more static elements for verification context
            context_purpose: Literal['action', 'verification'] = 'action' # New parameter
        ) -> Tuple[str, Dict[str, 'DOMElementNode']]:
        """
        Generates a string representation of VISIBLE elements tree for LLM context.
        Clearly distinguishes interactive elements (with index) from static ones.
        Assigns temporary IDs to static elements for later lookup.

        Args:
            include_attributes: List of specific attributes to include. If None, uses defaults.
            max_static_elements_action: Max static elements for 'action' purpose.
            max_static_elements_verification: Max static elements for 'verification' purpose.
            context_purpose: 'action' (concise) or 'verification' (more inclusive static).
            
        Returns:
            Tuple containing:
                - The formatted context string.
                - A dictionary mapping temporary static IDs (e.g., "s1", "s2")
                  to the corresponding DOMElementNode objects.

        """
        formatted_lines = []
        processed_node_ids = set()
        static_element_count = 0
        nodes_processed_count = 0 
        static_id_counter = 1 # Counter for temporary static IDs
        temp_static_id_map: Dict[str, 'DOMElementNode'] = {} # Map temporary ID to node

        max_static_elements = max_static_elements_verification if context_purpose == 'verification' else max_static_elements_action

        
        def get_direct_visible_text(node: DOMElementNode, max_len=10000) -> str:
            """Gets text directly within this node, ignoring children elements."""
            texts = []
            for child in node.children:
                if isinstance(child, DOMTextNode) and child.is_visible:
                    texts.append(child.text.strip())
            full_text = ' '.join(filter(None, texts))
            if len(full_text) > max_len:
                 return full_text[:max_len-3] + "..."
            return full_text

        def get_parent_hint(node: DOMElementNode) -> Optional[str]:
            """Gets a hint string for the nearest identifiable parent."""
            parent = node.parent
            if isinstance(parent, DOMElementNode):
                parent_attrs = parent.attributes
                hint_parts = []
                if parent_attrs.get('id'):
                    hint_parts.append(f"id=\"{parent_attrs['id'][:20]}\"") # Limit length
                if parent_attrs.get('data-testid'):
                    hint_parts.append(f"data-testid=\"{parent_attrs['data-testid'][:20]}\"")
                # Add class hint only if specific? Maybe too noisy. Start with id/testid.
                # if parent_attrs.get('class'):
                #    stable_classes = [c for c in parent_attrs['class'].split() if len(c) > 3 and not c.isdigit()]
                #    if stable_classes: hint_parts.append(f"class=\"{stable_classes[0][:15]}...\"") # Show first stable class

                if hint_parts:
                    return f"(inside: <{parent.tag_name} {' '.join(hint_parts)}>)"
            return None

        def process_node(node: Union['DOMElementNode', DOMTextNode], depth: int) -> None:
            nonlocal static_element_count, nodes_processed_count, static_id_counter # Allow modification

            # Skip if already processed or not an element
            if not isinstance(node, DOMElementNode): return
            nodes_processed_count += 1
            node_id = id(node)
            if node_id in processed_node_ids: return
            processed_node_ids.add(node_id)

            is_node_visible = node.is_visible
            visibility_marker = "" if is_node_visible else " (Not Visible)" 

            should_add_current_node = False
            line_to_add = ""
            is_interactive = node.highlight_index is not None
            temp_static_id_assigned = None # Track if ID was assigned to this node


            indent = '  ' * depth

            # --- Attribute Extraction (Common logic) ---
            attributes_to_show = {}
            default_attrs = ['id', 'name', 'class', 'aria-label', 'placeholder', 'role', 'type', 'value', 'title', 'alt', 'href', 'data-testid', 'data-value']
            attrs_to_check = include_attributes if include_attributes else default_attrs
            extract_attrs_for_this_node = is_interactive or (context_purpose == 'verification')
            if extract_attrs_for_this_node:
                for attr_key in attrs_to_check:
                    if attr_key in node.attributes and node.attributes[attr_key] is not None: # Check for not None
                        # Simple check to exclude extremely long class lists for brevity, unless it's ID/testid
                        if attr_key == 'class' and len(node.attributes[attr_key]) > 100 and context_purpose == 'action':
                            attributes_to_show[attr_key] = node.attributes[attr_key][:97] + "..."
                        else:
                            attributes_to_show[attr_key] = node.attributes[attr_key]
            attrs_str = ""
            if attributes_to_show:
                parts = []
                for key, value in attributes_to_show.items():
                    value_str = str(value) # Ensure it's a string
                    # Limit length for display
                    display_value = value_str if len(value_str) < 50 else value_str[:47] + '...'
                    # *** CORRECT HTML ESCAPING for attribute value strings ***
                    display_value = display_value.replace('&', '&').replace('<', '<').replace('>', '>').replace('"', '"')
                    parts.append(f'{key}="{display_value}"')
                attrs_str = " ".join(parts)

            # --- Format line based on Interactive vs. Static ---
            if is_interactive:
                # == INTERACTIVE ELEMENT == (Always include)
                text_content = node.get_all_text_till_next_clickable_element()
                text_content = ' '.join(text_content.split()) if text_content else ""
                # Truncate long text for display
                if len(text_content) > 150: text_content = text_content[:147] + "..."

                line_to_add = f"{indent}[{node.highlight_index}]<{node.tag_name}"
                if attrs_str: line_to_add += f" {attrs_str}"
                if text_content: line_to_add += f">{text_content}</{node.tag_name}>"
                else: line_to_add += " />"
                line_to_add += visibility_marker
                should_add_current_node = True

            elif static_element_count < max_static_elements:
                # == VISIBLE STATIC ELEMENT ==
                text_content = get_direct_visible_text(node)
                include_this_static = False

                # Determine if static node is relevant for verification
                if context_purpose == 'verification':
                    common_static_tags = {'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'span', 'div', 'li', 'label', 'td', 'th', 'strong', 'em', 'dt', 'dd'}
                    # Include if common tag OR has text OR *has attributes calculated in attrs_str*
                    if node.tag_name in common_static_tags or text_content or attrs_str:
                        include_this_static = True

                if include_this_static:
                    # --- Assign temporary static ID ---
                    current_static_id = f"s{static_id_counter}"
                    temp_static_id_map[current_static_id] = node
                    temp_static_id_assigned = current_static_id # Mark that ID was assigned
                    static_id_counter += 1
                    
                    # *** Start building the line ***
                    line_to_add = f"{indent}<{node.tag_name}"

                    # *** CRUCIAL: Add the calculated attributes string ***
                    if attrs_str:
                        line_to_add += f" {attrs_str}"
                        
                    # --- Add the static ID attribute to the string ---
                    line_to_add += f' data-static-id="{current_static_id}"'

                    # *** Add the static marker ***
                    line_to_add += " (Static)"
                    line_to_add += visibility_marker

                    # *** Add parent hint ONLY if element lacks key identifiers ***
                    node_attrs = node.attributes # Use original attributes for this check
                    has_key_identifier = node_attrs.get('id') or node_attrs.get('data-testid') or node_attrs.get('name')
                    if not has_key_identifier:
                            parent_hint = get_parent_hint(node)
                            if parent_hint:
                                line_to_add += f" {parent_hint}"

                    # *** Add text content and close tag ***
                    if text_content:
                        line_to_add += f">{text_content}</{node.tag_name}>"
                    else:
                        line_to_add += " />"

                    should_add_current_node = True
                    static_element_count += 1

            # --- Add the formatted line if needed ---
            if should_add_current_node:
                formatted_lines.append(line_to_add)
                # logger.debug(f"Added line: {line_to_add}") # Optional debug

            # --- ALWAYS Recurse into children (unless static limit hit) ---
            # We recurse even if the parent wasn't added, because children might be visible/interactive
            if static_element_count >= max_static_elements:
                 # Stop recursing down static branches if limit is hit
                 pass
            else:
                 for child in node.children:
                     # Pass DOMElementNode or DOMTextNode directly
                     process_node(child, depth + 1)


        # Start processing from the root element
        process_node(self, 0)

        # logger.debug(f"Finished generate_llm_context_string. Processed {nodes_processed_count} nodes. Added {len(formatted_lines)} lines.") # Log summary

        output_str = '\n'.join(formatted_lines)
        if static_element_count >= max_static_elements:
             output_str += f"\n{ '  ' * 0 }... (Static element list truncated after {max_static_elements} entries)"
        return output_str, temp_static_id_map


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