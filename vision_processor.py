# vision_processor.py
from llm_client import LLMClient
import logging
from typing import Optional

logger = logging.getLogger(__name__)

class VisionProcessor:
    """Uses LLM Vision to analyze screenshots for web navigation."""

    def __init__(self, llm_client: LLMClient):
        self.llm_client = llm_client
        logger.info("VisionProcessor initialized.")

    def analyze_screenshot_for_action(self, image_bytes: bytes, task_prompt: str, failed_selector: Optional[str] = None, error_context: Optional[str] = None) -> str:
        """
        Analyzes a screenshot specifically to help decide the next action,
        especially after a failure.

        Args:
            image_bytes: The screenshot image data as bytes.
            task_prompt: The specific subtask the agent is trying to achieve.
            failed_selector: The selector that previously failed (if any).
            error_context: The error message from the failed action (if any).

        Returns:
            The textual analysis focused on actionable elements.
        """
        if not image_bytes:
            logger.warning("No image bytes provided for vision analysis.")
            return "Error: No screenshot available for analysis."

        logger.info(f"Analyzing screenshot for task: '{task_prompt}'")
        if failed_selector:
            logger.info(f"Context: Previous attempt failed for selector '{failed_selector}' with error: {error_context}")

        # Construct a more focused prompt for the vision model
        full_prompt = f"""Analyze the provided webpage screenshot to help achieve the current subtask: '{task_prompt}'.

Focus on identifying interactive elements (buttons, links, inputs, dropdowns etc.) visually relevant to this subtask.
"""
        if failed_selector:
             full_prompt += f"\nA previous attempt to use the selector `{failed_selector}` failed, possibly because it was incorrect, the element wasn't visible/interactive, or the page structure changed. The error was: {error_context}.\n"
             full_prompt += f"\nBased on the visual layout AND considering the HTML context provided separately (which contains reference `ai-id`s), suggest alternative, potentially more robust CSS selectors using **NATIVE attributes** (id, name, data-testid, aria-label, placeholder, unique visible text, class combinations) for the element needed for the task '{task_prompt}'. Pay attention to visual cues like text, icons, color, and position."
        else:
             full_prompt += "\nDescribe the key interactive elements visually and suggest robust CSS selectors using **NATIVE attributes**."

        full_prompt += """
For each highly relevant interactive element, provide:
1. A concise visual description (e.g., "blue button labeled 'Log In' in the top right corner").
2. Its approximate location.
3. Suggest a robust Playwright CSS selector using **NATIVE attributes**. Prioritize attributes like `id`, `data-testid`, `name`, `aria-label`, `placeholder`. If using text, use `element:has-text("Exact Text")`. **Do not suggest selectors based on `data-ai-id`.**

Also, based on the visual, state if any previous actions (like typing text) seem to have visually succeeded or failed on the page.
**Output only the analysis and selector suggestions.** Do not try to decide the next action yourself.
"""
        # Removed: DO NOT just describe the whole page. Focus on the task and actionable elements/selectors.

        analysis = self.llm_client.generate_multimodal(full_prompt, image_bytes)
        logger.info("Received screenshot analysis from LLM Vision.")
        # logger.debug(f"Vision Analysis:\n{analysis}")
        return analysis