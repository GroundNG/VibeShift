# html_processor.py
from bs4 import BeautifulSoup, Comment
import logging

logger = logging.getLogger(__name__)

class HTMLProcessor:
    # ... (INTERACTIVE_TAGS, RELEVANT_ATTRS remain same) ...
    INTERACTIVE_TAGS = [
        'a', 'button', 'input', 'textarea', 'select', 'option',
        'details', 'summary', # For dropdown/accordions
        '[role="button"]', '[role="link"]', '[role="menuitem"]',
        '[role="tab"]', '[role="checkbox"]', '[role="radio"]',
        '[onclick]', '[tabindex]:not([tabindex="-1"])'
    ]
    RELEVANT_ATTRS = ['id', 'name', 'class', 'type', 'value', 'placeholder', 'aria-label',
                      'aria-labelledby', 'role', 'title', 'alt', 'for', 'data-testid']


    def __init__(self):
        logger.info("HTMLProcessor initialized.")

    def _get_element_summary(self, element, ai_id: int) -> str:
        """Creates a concise summary string for an element, including native attrs and ai-id."""
        attrs_str_parts = []
        # Prioritize showing the most useful native attributes first
        for attr in ['id', 'name', 'class', 'data-testid', 'aria-label', 'placeholder', 'type', 'role', 'value', 'title']:
            if element.has_attr(attr):
                val = ""
                # Handle class attribute specially (it's a list)
                if attr == 'class':
                    val = ' '.join(element.get(attr, [])) # Join list of classes
                else:
                    val = element.get(attr, '')

                if not val: continue # Skip empty attributes

                # Shorten long values if necessary
                val_display = val if len(val) < 50 else val[:47] + '...'
                attrs_str_parts.append(f'{attr}="{val_display}"')

        # Get text content, limited length
        text = element.get_text(strip=True)[:80]
        if len(text) == 80: text += '...'
        text_summary = f' text="{text}"' if text else ""

        summary = f'ai-id={ai_id}: <{element.name}'
        if attrs_str_parts:
             summary += ' ' + ' '.join(attrs_str_parts)
        summary += f'>{text_summary}'
        return summary


    def clean_html(self, html_content: str) -> str:
        """
        Cleans HTML and generates a summary of interactive elements
        with native attributes and a generated ai-id for reference.
        """
        # ... (initial setup, try/except block) ...
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            # ... (remove script, style, comment, etc.) ...
            for tag in soup(['script', 'style', 'link', 'meta', 'noscript', 'head']):
                 tag.decompose()
            for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
                 comment.extract()


            # Add unique IDs and create summary
            interactive_elements_summary = []
            css_selector = ', '.join(self.INTERACTIVE_TAGS)
            try:
                 elements = soup.select(css_selector)
            except Exception as e:
                 logger.warning(f"CSS Selector failed during HTML cleaning: {e}. Falling back.")
                 elements = []

            ai_id_counter = 1
            for element in elements:
                 # Basic visibility check (can be improved)
                 parent_style = element.parent.get('style', '') if element.parent else ''
                 element_style = element.get('style', '')
                 is_hidden = 'display:none' in parent_style or 'display:none' in element_style \
                           or element.get('hidden') or (element.parent and element.parent.get('hidden')) \
                           or element.get('tabindex') == '-1'

                 if not is_hidden:
                     # Add the ai-id attribute to the element in the soup
                     element['data-ai-id'] = str(ai_id_counter)
                     # Generate the summary string using the helper
                     summary = self._get_element_summary(element, ai_id_counter)
                     interactive_elements_summary.append(summary)
                     ai_id_counter += 1

            # Get the modified HTML structure *after* adding data-ai-id attributes
            cleaned_html_structure = soup.prettify()
            summary_str = "\n".join(interactive_elements_summary)

            logger.info(f"HTML cleaned. Generated summary for {ai_id_counter - 1} interactive elements.")

            # Combine summary and structure
            MAX_HTML_LEN = 18000 # Allow slightly more context
            final_output = (
                f"INTERACTIVE ELEMENTS SUMMARY (ai-id added for reference):\n{summary_str}\n\n"
                f"FULL CLEANED HTML (contains elements with data-ai-id attributes):\n{cleaned_html_structure}"
            )
            if len(final_output) > MAX_HTML_LEN:
                 cutoff = len(final_output) - MAX_HTML_LEN
                 # Prioritize keeping the summary and the start of the HTML
                 summary_len = len(summary_str) + 100 # Approx length of headers
                 html_cutoff_point = MAX_HTML_LEN - summary_len
                 final_output = (
                      f"INTERACTIVE ELEMENTS SUMMARY (ai-id added for reference):\n{summary_str}\n\n"
                      f"FULL CLEANED HTML (contains elements with data-ai-id attributes):\n"
                      f"{cleaned_html_structure[:html_cutoff_point]}\n"
                      f"... (HTML truncated by approx {cutoff} chars)"
                 )

            return final_output

        except Exception as e:
            # Fallback: return basic text
            logger.error(f"Error cleaning HTML: {e}", exc_info=True)
            try:
                 raw_soup = BeautifulSoup(html_content, 'html.parser')
                 return raw_soup.get_text(separator='\n', strip=True)[:15000]
            except Exception as fallback_e:
                  logger.error(f"Fallback HTML text extraction failed: {fallback_e}", exc_info=True)
                  return "Error: Could not process HTML content."