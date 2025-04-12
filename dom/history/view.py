# dom/history/view.py (Copied and imports/typing adjusted)
from dataclasses import dataclass
from typing import Optional, List, Dict # Added Dict

# Use Pydantic for coordinate models if available and desired
try:
    from pydantic import BaseModel, Field

    class Coordinates(BaseModel):
        x: float # Use float for potential subpixel values
        y: float

    class CoordinateSet(BaseModel):
        # Match names from buildDomTree.js if they differ
        top_left: Coordinates
        top_right: Coordinates
        bottom_left: Coordinates
        bottom_right: Coordinates
        center: Coordinates
        width: float
        height: float

    class ViewportInfo(BaseModel):
        scroll_x: float = Field(alias="scrollX") # Match JS key if needed
        scroll_y: float = Field(alias="scrollY")
        width: float
        height: float

except ImportError:
    # Fallback if Pydantic is not installed (less type safety)
    Coordinates = Dict[str, float]
    CoordinateSet = Dict[str, Union[Coordinates, float]]
    ViewportInfo = Dict[str, float]
    BaseModel = object # Placeholder
    logger.warning("Pydantic not found. Using basic dicts for coordinate models.")


@dataclass
class HashedDomElement:
    """ Hash components of a DOM element for comparison. """
    branch_path_hash: str
    attributes_hash: str
    xpath_hash: str
    # text_hash: str (Still excluded)

@dataclass
class DOMHistoryElement:
    """ A serializable representation of a DOM element's state at a point in time. """
    tag_name: str
    xpath: str
    highlight_index: Optional[int]
    entire_parent_branch_path: List[str]
    attributes: Dict[str, str]
    shadow_root: bool = False
    css_selector: Optional[str] = None # Generated enhanced selector
    # Store the Pydantic models or dicts directly
    page_coordinates: Optional[CoordinateSet] = None
    viewport_coordinates: Optional[CoordinateSet] = None
    viewport_info: Optional[ViewportInfo] = None

    def to_dict(self) -> dict:
        """ Converts the history element to a dictionary. """
        data = {
            'tag_name': self.tag_name,
            'xpath': self.xpath,
            'highlight_index': self.highlight_index,
            'entire_parent_branch_path': self.entire_parent_branch_path,
            'attributes': self.attributes,
            'shadow_root': self.shadow_root,
            'css_selector': self.css_selector,
             # Handle Pydantic models correctly if used
            'page_coordinates': self.page_coordinates.model_dump() if isinstance(self.page_coordinates, BaseModel) else self.page_coordinates,
            'viewport_coordinates': self.viewport_coordinates.model_dump() if isinstance(self.viewport_coordinates, BaseModel) else self.viewport_coordinates,
            'viewport_info': self.viewport_info.model_dump() if isinstance(self.viewport_info, BaseModel) else self.viewport_info,
        }
        # Filter out None values if desired
        # return {k: v for k, v in data.items() if v is not None}
        return data