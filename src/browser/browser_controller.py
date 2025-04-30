# /src/browser_controller.py
from playwright.sync_api import sync_playwright, Page, Browser, Playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError, Locator, ConsoleMessage, expect
import logging
import time
import random
import json
import os
from typing import Optional, Any, Dict, List, Callable, Tuple
import threading
import platform

from ..dom.service import DomService
from ..dom.views import DOMState, DOMElementNode, SelectorMap

logger = logging.getLogger(__name__)

COMMON_HEADERS = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br',
    'Accept-Language': 'en-US,en;q=0.9',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Upgrade-Insecure-Requests': '1',
}

HIDE_WEBDRIVER_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {
  get: () => undefined
});
"""

# --- JavaScript for click listener and selector generation ---
CLICK_LISTENER_JS = """
async () => {
    // Reset/Initialize the global flag/variable
  window._recorder_override_selector = undefined;
  console.log('[Recorder Listener] Attaching click listener...');
  const PANEL_ID = 'bw-recorder-panel';
  
  const clickHandler = async (event) => {
    const targetElement = event.target;
    let isPanelClick = false; // Flag to track if click is inside the panel
    
    // Check if the click target is the panel or inside the panel
    // using element.closest()
    if (targetElement && targetElement.closest && targetElement.closest(`#${PANEL_ID}`)) {
        console.log('[Recorder Listener] Click inside panel detected. Allowing event to proceed normally.');
        isPanelClick = true;
        // DO ABSOLUTELY NOTHING HERE - let the event continue to the button's own listener
    }
    
    // --- Only process as an override attempt if it was NOT a panel click ---
    if (!isPanelClick) {
        console.log('[Recorder Listener] Click detected (Outside panel)! Processing as override.');
        event.preventDefault(); // Prevent default action (like navigation) ONLY for override clicks
        event.stopPropagation(); // Stop propagation ONLY for override clicks

        if (!targetElement) {
          console.warn('[Recorder Listener] Override click event has no target.');
          // Remove listener even if target is null to avoid getting stuck
          document.body.removeEventListener('click', clickHandler, { capture: true });
          console.log('[Recorder Listener] Listener removed due to null target.');
          return;
        }
    

        // --- Simple Selector Generation (enhance as needed) ---
        let selector = '';
        function escapeCSS(value) {
            if (!value) return '';
            // Basic escape for common CSS special chars in identifiers/strings
            // For robust escaping, a library might be better, but this covers many cases.
            return value.replace(/([!"#$%&'()*+,./:;<=>?@\\[\\]^`{|}~])/g, '\\$1');
        }

        if (targetElement.id && targetElement.id !== PANEL_ID && targetElement.id !== 'playwright-highlight-container') {
        selector = `#${escapeCSS(targetElement.id.trim())}`;
        } else if (targetElement.getAttribute('data-testid')) {
        selector = `[data-testid="${escapeCSS(targetElement.getAttribute('data-testid').trim())}"]`;
        } else if (targetElement.name) {
        selector = `${targetElement.tagName.toLowerCase()}[name="${escapeCSS(targetElement.name.trim())}"]`;
        } else {
        // Fallback: Basic XPath -> CSS approximation (needs improvement)
        let path = '';
        let current = targetElement;
        while (current && current.tagName && current.tagName.toLowerCase() !== 'body' && current.parentNode) {
            let segment = current.tagName.toLowerCase();
            const parent = current.parentElement;
            if (parent) {
                const siblings = Array.from(parent.children);
                const sameTagSiblings = siblings.filter(sib => sib.tagName === current.tagName);
                if (sameTagSiblings.length > 1) {
                    let index = 1;
                    for(let i=0; i < sameTagSiblings.length; i++) { // Find index correctly
                        if(sameTagSiblings[i] === current) {
                            index = i + 1;
                            break;
                        }
                    }
                    // Prefer nth-child if possible, might be slightly more stable
                    try {
                        const siblingIndex = Array.prototype.indexOf.call(parent.children, current) + 1;
                        segment += `:nth-child(${siblingIndex})`;
                    } catch(e) { // Fallback if indexOf fails
                        segment += `:nth-of-type(${index})`;
                    }
                }
            }
            path = segment + (path ? ' > ' + path : '');
            current = parent;
        }
        selector = path ? `body > ${path}` : targetElement.tagName.toLowerCase();
        console.log(`[Recorder Listener] Generated fallback selector: ${selector}`);
        }
        // --- End Selector Generation ---

        console.log(`[Recorder Listener] Override Target: ${targetElement.tagName}, Generated selector: ${selector}`);

        // Only set override if a non-empty selector was generated
        if (selector) {
            window._recorder_override_selector = selector;
            console.log('[Recorder Listener] Override selector variable set.');
        } else {
             console.warn('[Recorder Listener] Could not generate a valid selector for the override click.');
        }
        
        // ---- IMPORTANT: Remove the listener AFTER processing an override click ----
        // This prevents it interfering further and ensures it's gone before panel interaction waits
        document.body.removeEventListener('click', clickHandler, { capture: true });
        console.log('[Recorder Listener] Listener removed after processing override click.');
    };
    // If it WAS a panel click (isPanelClick = true), we did nothing in this handler.
    // The event continues to the button's specific onclick handler.
    // The listener remains attached to the body for subsequent clicks outside the panel.
  };
  // --- Add listener ---
  // Ensure no previous listener exists before adding a new one
  if (window._recorderClickListener) {
      console.warn('[Recorder Listener] Removing potentially lingering listener before attaching new one.');
      document.body.removeEventListener('click', window._recorderClickListener, { capture: true });
  }
  // Add listener in capture phase to catch clicks first
  document.body.addEventListener('click', clickHandler, { capture: true });
  window._recorderClickListener = clickHandler; // Store reference to remove later
}
"""

RECORDER_PANEL_JS = """
() => {
    const PANEL_ID = 'bw-recorder-panel';
    const INPUT_ID = 'bw-recorder-param-input'; // Used for general param input now
    const PARAM_BTN_ID = 'bw-recorder-param-button'; // Button next to general param input
    const PARAM_CONT_ID = 'bw-recorder-param-container'; // Container for single param input
    const ASSERT_PARAM_INPUT1_ID = 'bw-assert-param1';
    const ASSERT_PARAM_INPUT2_ID = 'bw-assert-param2';
    const ASSERT_PARAM_CONT_ID = 'bw-assert-param-container'; // Container for assertion-specific params

    // --- Function to create or get the panel ---
    function getOrCreatePanel() {
        let panel = document.getElementById(PANEL_ID);
        if (!panel) {
            panel = document.createElement('div');
            panel.id = PANEL_ID;
            // Basic Styling (customize as needed)
            Object.assign(panel.style, {
                position: 'fixed',
                bottom: '10px',
                right: '10px',
                padding: '10px',
                background: 'rgba(40, 40, 40, 0.9)',
                color: 'white',
                border: '1px solid #ccc',
                borderRadius: '5px',
                zIndex: '2147483647', // Max z-index
                fontFamily: 'sans-serif',
                fontSize: '12px',
                boxShadow: '0 2px 5px rgba(0,0,0,0.3)',
                display: 'none', // Initially hidden
                pointerEvents: 'none'
            });
            document.body.appendChild(panel);
        }
        return panel;
    }
    
    // --- Helper to Set Button Listeners ---
    // (choiceValue is what window._recorder_user_choice will be set to)
    function setChoiceOnClick(buttonId, choiceValue) {
        const btn = document.getElementById(buttonId);
        if (btn) {
            btn.onclick = () => { window._recorder_user_choice = choiceValue; };
        } else {
             console.warn(`[Recorder Panel] Button with ID ${buttonId} not found for listener.`);
        }
    }
    
    // State 1: Confirm/Override Assertion Target
    window._recorder_showAssertionTargetPanel = (plannedDesc, suggestedSelector) => {
        const panel = getOrCreatePanel();
        const selectorDisplay = suggestedSelector ? `<code>${suggestedSelector.substring(0, 100)}...</code>` : '<i>AI could not suggest a target.</i>';
        panel.innerHTML = `
            <div style="margin-bottom: 5px; font-weight: bold; pointer-events: auto;">Define Assertion:</div>
            <div style="margin-bottom: 8px; max-width: 300px; word-wrap: break-word; pointer-events: auto;">${plannedDesc}</div>
            <div style="margin-bottom: 5px; font-style: italic; pointer-events: auto;">Suggested Target Selector: ${selectorDisplay}</div>
            <button id="bw-assert-confirm-target" style="margin: 2px; padding: 3px 6px; pointer-events: auto;" ${!suggestedSelector ? 'disabled' : ''}>Use Suggested</button>
            <button id="bw-assert-override-target" style="margin: 2px; padding: 3px 6px; pointer-events: auto;">Click New Target</button>
            <button id="bw-assert-skip" style="margin: 2px; padding: 3px 6px; pointer-events: auto;">Skip Assertion</button>
            <button id="bw-abort-btn" style="margin: 2px; padding: 3px 6px; background-color: #d9534f; color: white; border: none; pointer-events: auto;">Abort</button>
        `;
        window._recorder_user_choice = undefined; // Reset choice
        setChoiceOnClick('bw-assert-confirm-target', 'confirm_target');
        setChoiceOnClick('bw-assert-override-target', 'override_target');
        setChoiceOnClick('bw-assert-skip', 'skip');
        setChoiceOnClick('bw-abort-btn', 'abort');
        panel.style.display = 'block';
        console.log('[Recorder Panel] Assertion Target Panel Shown.');
    };
    
    // State 2: Select Assertion Type
    window._recorder_showAssertionTypePanel = (targetSelector) => {
        const panel = getOrCreatePanel();
        panel.innerHTML = `
             <div style="margin-bottom: 5px; font-weight: bold; pointer-events: auto;">Select Assertion Type:</div>
             <div style="margin-bottom: 8px; font-size: 11px; pointer-events: auto;">Target: <code>${targetSelector.substring(0, 100)}...</code></div>
             <div style="display: flex; flex-wrap: wrap; gap: 5px; pointer-events: auto;">
                 <button id="type-contains" style="padding: 3px 6px; pointer-events: auto;">Text Contains</button>
                 <button id="type-equals" style="padding: 3px 6px; pointer-events: auto;">Text Equals</button>
                 <button id="type-visible" style="padding: 3px 6px; pointer-events: auto;">Is Visible</button>
                 <button id="type-hidden" style="padding: 3px 6px; pointer-events: auto;">Is Hidden</button>
                 <button id="type-attr" style="padding: 3px 6px; pointer-events: auto;">Attribute Equals</button>
                 <button id="type-count" style="padding: 3px 6px; pointer-events: auto;">Element Count</button>
                 <button id="type-checked" style="padding: 3px 6px; pointer-events: auto;">Is Checked</button>
                 <button id="type-not-checked" style="padding: 3px 6px; pointer-events: auto;">Not Checked</button>
             </div>
             <hr style="margin: 8px 0; border-top: 1px solid #555;">
             <button id="bw-assert-back-target" style="margin-right: 5px; padding: 3px 6px; pointer-events: auto;">< Back (Target)</button>
             <button id="bw-assert-skip" style="margin-right: 5px; padding: 3px 6px; pointer-events: auto;">Skip Assertion</button>
             <button id="bw-abort-btn" style="padding: 3px 6px; background-color: #d9534f; color: white; border: none; pointer-events: auto;">Abort</button>
        `;
        window._recorder_user_choice = undefined; // Reset choice
        // Set listeners for type selection
        setChoiceOnClick('type-contains', 'select_type_text_contains');
        setChoiceOnClick('type-equals', 'select_type_text_equals');
        setChoiceOnClick('type-visible', 'select_type_visible');
        setChoiceOnClick('type-hidden', 'select_type_hidden');
        setChoiceOnClick('type-attr', 'select_type_attribute_equals');
        setChoiceOnClick('type-count', 'select_type_element_count');
        setChoiceOnClick('type-checked', 'select_type_checked');
        setChoiceOnClick('type-not-checked', 'select_type_not_checked');
        // Other controls
        setChoiceOnClick('bw-assert-back-target', 'back_to_target');
        setChoiceOnClick('bw-assert-skip', 'skip');
        setChoiceOnClick('bw-abort-btn', 'abort');
        panel.style.display = 'block';
        console.log('[Recorder Panel] Assertion Type Panel Shown.');
    };

    // State 3: Enter Assertion Parameters
    window._recorder_showAssertionParamsPanel = (targetSelector, assertionType, paramLabels) => {
        // paramLabels is an array like ['Expected Text'] or ['Attribute Name', 'Expected Value'] or ['Expected Count']
        const panel = getOrCreatePanel();
        let inputHTML = '';
        if (paramLabels.length === 1) {
            inputHTML = `<label for="${ASSERT_PARAM_INPUT1_ID}" style="margin-right: 5px; pointer-events: auto;">${paramLabels[0]}:</label>
                         <input type="text" id="${ASSERT_PARAM_INPUT1_ID}" style="padding: 2px 4px; width: 180px; pointer-events: auto;">`;
        } else if (paramLabels.length === 2) {
             inputHTML = `<div style="margin-bottom: 3px;">
                              <label for="${ASSERT_PARAM_INPUT1_ID}" style="display: inline-block; width: 100px; pointer-events: auto;">${paramLabels[0]}:</label>
                              <input type="text" id="${ASSERT_PARAM_INPUT1_ID}" style="padding: 2px 4px; width: 120px; pointer-events: auto;">
                          </div>
                          <div>
                              <label for="${ASSERT_PARAM_INPUT2_ID}" style="display: inline-block; width: 100px; pointer-events: auto;">${paramLabels[1]}:</label>
                              <input type="text" id="${ASSERT_PARAM_INPUT2_ID}" style="padding: 2px 4px; width: 120px; pointer-events: auto;">
                          </div>`;
        }

        panel.innerHTML = `
             <div style="margin-bottom: 5px; font-weight: bold; pointer-events: auto;">Enter Parameters:</div>
             <div style="margin-bottom: 3px; font-size: 11px; pointer-events: auto;">Target: <code>${targetSelector.substring(0, 60)}...</code></div>
             <div style="margin-bottom: 8px; font-size: 11px; pointer-events: auto;">Assertion: ${assertionType}</div>
             <div id="${ASSERT_PARAM_CONT_ID}" style="margin-bottom: 8px; pointer-events: auto;">
                ${inputHTML}
             </div>
             <button id="bw-assert-record" style="margin-right: 5px; padding: 3px 6px; pointer-events: auto;">Record Assertion</button>
             <button id="bw-assert-back-type" style="margin-right: 5px; padding: 3px 6px; pointer-events: auto;">< Back (Type)</button>
             <button id="bw-abort-btn" style="padding: 3px 6px; background-color: #d9534f; color: white; border: none; pointer-events: auto;">Abort</button>
        `;
        window._recorder_user_choice = undefined; // Reset choice
        setChoiceOnClick('bw-assert-record', 'submit_params');
        setChoiceOnClick('bw-assert-back-type', 'back_to_type');
        setChoiceOnClick('bw-abort-btn', 'abort');
        panel.style.display = 'block';
        // Auto-focus the first input if possible
        const firstInput = document.getElementById(ASSERT_PARAM_INPUT1_ID);
        if (firstInput) {
             setTimeout(() => firstInput.focus(), 50); // Short delay
        }
        console.log('[Recorder Panel] Assertion Params Panel Shown.');
    };
    
    // State 4: Verification Review
    window._recorder_showVerificationReviewPanel = (args) => {
        const { plannedDesc, aiVerified, aiReasoning, assertionType, parameters, selector } = args;
        const panel = getOrCreatePanel();
        let detailsHTML = '';
        let recordButtonDisabled = true; // Disable record button by default

        // --- Build Details Section based on AI Result ---
        if (aiVerified) {
            // Check if we have enough info to actually record the assertion
            const canRecord = assertionType && selector;
            recordButtonDisabled = !canRecord;

            detailsHTML += `<div style="margin-bottom: 3px; pointer-events: auto;">Assertion: <code>${assertionType || 'N/A'}</code></div>`;
            detailsHTML += `<div style="margin-bottom: 3px; pointer-events: auto;">Selector: <code>${selector ? selector.substring(0, 100) + '...' : 'MISSING!'}</code></div>`;
            // Safely format parameters (convert object to string)
            let paramsString = 'None';
            if (parameters && Object.keys(parameters).length > 0) {
                 try { paramsString = JSON.stringify(parameters); } catch(e){ paramsString = '{...}'; }
            }
            detailsHTML += `<div style="margin-bottom: 5px; pointer-events: auto;">Parameters: <code>${paramsString}</code></div>`;
             if (!canRecord) {
                detailsHTML += `<div style="color: #ffcc00; font-size: 11px; pointer-events: auto;">Warning: Cannot record assertion directly (missing type or selector from AI). Choose Manual or Skip.</div>`;
            }
        } else {
            // Verification failed
             detailsHTML += `<div style="color: #ffdddd; pointer-events: auto;">AI could not verify the condition.</div>`;
        }


        panel.innerHTML = `
            <div style="margin-bottom: 5px; font-weight: bold; pointer-events: auto;">AI Verification Review:</div>
            <div style="margin-bottom: 8px; max-width: 300px; word-wrap: break-word; pointer-events: auto;">${plannedDesc}</div>
            <div style="margin-bottom: 5px; font-style: italic; color: ${aiVerified ? '#ccffcc' : '#ffdddd'}; pointer-events: auto;">
                AI Result: ${aiVerified ? 'PASSED' : 'FAILED'}
            </div>
            <div style="margin-bottom: 8px; font-size: 11px; max-height: 60px; overflow-y: auto; border: 1px dashed #666; padding: 3px; pointer-events: auto;">
                AI Reasoning: ${aiReasoning || 'N/A'}
            </div>
            ${detailsHTML}
            <hr style="margin: 8px 0; border-top: 1px solid #555;">
            <button id="bw-verify-record" style="margin: 2px; padding: 3px 6px; pointer-events: auto;" ${recordButtonDisabled ? 'disabled title="Cannot record directly, missing info from AI"' : ''}>Record AI Assertion</button>
            <button id="bw-verify-manual" style="margin: 2px; padding: 3px 6px; pointer-events: auto;">Define Manually</button>
            <button id="bw-verify-skip" style="margin: 2px; padding: 3px 6px; pointer-events: auto;">Skip Step</button>
            <button id="bw-abort-btn" style="margin: 2px; padding: 3px 6px; background-color: #d9534f; color: white; border: none; pointer-events: auto;">Abort</button>
             <!-- Re-use existing parameterization container, initially hidden -->
             <div id="${PARAM_CONT_ID}" style="margin-top: 8px; display: none; pointer-events: auto;">
                 <input type="text" id="${INPUT_ID}" placeholder="Parameter Name (optional)" style="padding: 2px 4px; width: 150px; margin-right: 5px; pointer-events: auto;">
                 <button id="${PARAM_BTN_ID}" style="padding: 3px 6px; pointer-events: auto;">Set Param & Record</button>
             </div>
        `;
        window._recorder_user_choice = undefined; // Reset choice
        window._recorder_parameter_name = undefined; // Reset param name

        // Set listeners
        setChoiceOnClick('bw-verify-record', 'record_ai');
        setChoiceOnClick('bw-verify-manual', 'define_manual');
        setChoiceOnClick('bw-verify-skip', 'skip');
        setChoiceOnClick('bw-abort-btn', 'abort');
        // Listener for the parameterization button (same as before)
        const paramBtn = document.getElementById(PARAM_BTN_ID);
        if (paramBtn) {
            paramBtn.onclick = () => {
                const inputVal = document.getElementById(INPUT_ID).value.trim();
                window._recorder_parameter_name = inputVal ? inputVal : null;
                window._recorder_user_choice = 'parameterized'; // Special choice
            };
        }

        panel.style.display = 'block';
        console.log('[Recorder Panel] Verification Review Panel Shown.');
    };

    // Function to retrieve assertion parameters
    window._recorder_getAssertionParams = (count) => {
        const params = {};
        const input1 = document.getElementById(ASSERT_PARAM_INPUT1_ID);
        if (input1) params.param1 = input1.value;
        if (count > 1) {
             const input2 = document.getElementById(ASSERT_PARAM_INPUT2_ID);
             if (input2) params.param2 = input2.value;
        }
        console.log('[Recorder Panel] Retrieved assertion params:', params);
        return params;
    };

    // --- Function to update panel content ---
    window._recorder_showPanel = (stepDescription, suggestionText) => {
        const panel = getOrCreatePanel();
        panel.innerHTML = `
            <div style="margin-bottom: 5px; font-weight: bold; pointer-events: auto;">Next Step:</div> <!-- Re-enable for text selection if needed -->
            <div style="margin-bottom: 8px; max-width: 300px; word-wrap: break-word; pointer-events: auto;">${stepDescription}</div>
            <div style="margin-bottom: 5px; font-style: italic; pointer-events: auto;">AI Suggests: ${suggestionText}</div>
            <button id="bw-accept-btn" style="margin-right: 5px; padding: 3px 6px; pointer-events: auto;">Accept Suggestion</button> <!-- <<< Re-enable pointer events for buttons -->
            <button id="bw-skip-btn" style="margin-right: 5px; padding: 3px 6px; pointer-events: auto;">Skip Step</button> <!-- <<< Re-enable pointer events for buttons -->
            <button id="bw-abort-btn" style="padding: 3px 6px; background-color: #d9534f; color: white; border: none; pointer-events: auto;">Abort</button> <!-- <<< Re-enable pointer events for buttons -->
            <div id="${PARAM_CONT_ID}" style="margin-top: 8px; display: none; pointer-events: auto;"> <!-- <<< Re-enable pointer events for container -->
                 <input type="text" id="${INPUT_ID}" placeholder="Parameter Name (optional)" style="padding: 2px 4px; width: 150px; margin-right: 5px; pointer-events: auto;"> <!-- <<< Re-enable pointer events for input -->
                 <button id="${PARAM_BTN_ID}" style="padding: 3px 6px; pointer-events: auto;">Set Param & Record</button> <!-- <<< Re-enable pointer events for buttons -->
            </div>
        `;

        // --- Attach Button Listeners ---
        // Reset choice flag before showing
        window._recorder_user_choice = undefined;
        window._recorder_parameter_name = undefined;

        document.getElementById('bw-accept-btn').onclick = () => { window._recorder_user_choice = 'accept'; };
        document.getElementById('bw-skip-btn').onclick = () => { window._recorder_user_choice = 'skip'; /* hidePanel(); */ }; // Optionally hide immediately
        document.getElementById('bw-abort-btn').onclick = () => { window._recorder_user_choice = 'abort'; /* hidePanel(); */ };
        document.getElementById(PARAM_BTN_ID).onclick = () => {
            const inputVal = document.getElementById(INPUT_ID).value.trim();
            window._recorder_parameter_name = inputVal ? inputVal : null; // Store null if empty
            window._recorder_user_choice = 'parameterized'; // Special choice for parameterization submit
            // Don't hide panel here, Python side handles it after retrieving value
        };

        panel.style.display = 'block'; // Make panel visible
        console.log('[Recorder Panel] Panel shown.');
    };

    // --- Function to hide the panel ---
    window._recorder_hidePanel = () => {
        const panel = document.getElementById(PANEL_ID);
        if (panel) {
            panel.style.display = 'none';
            console.log('[Recorder Panel] Panel hidden.');
        }
         // Also reset choice on hide just in case
        window._recorder_user_choice = undefined;
        window._recorder_parameter_name = undefined;
    };

    // --- Function to show parameterization UI ---
    window._recorder_showParamUI = (defaultValue) => {
         const paramContainer = document.getElementById(PARAM_CONT_ID);
         const inputField = document.getElementById(INPUT_ID);
         const acceptBtn = document.getElementById('bw-accept-btn');
         if(paramContainer && inputField && acceptBtn) {
             inputField.value = ''; // Clear previous value
             inputField.setAttribute('placeholder', `Param Name for '${defaultValue.substring(0,20)}...' (optional)`);
             paramContainer.style.display = 'block';
             // Hide the original "Accept" button, show param button
             acceptBtn.style.display = 'none';
             document.getElementById(PARAM_BTN_ID).style.display = 'inline-block'; // Ensure param button is visible
             console.log('[Recorder Panel] Parameterization UI shown.');
             return true;
         }
         console.error('[Recorder Panel] Could not find parameterization elements.');
         return false;
     };

    // --- Function to remove the panel ---
    window._recorder_removePanel = () => {
        const panel = document.getElementById(PANEL_ID);
        if (panel) {
            panel.remove();
            console.log('[Recorder Panel] Panel removed.');
        }
        // Clean up global flags
        delete window._recorder_user_choice;
        delete window._recorder_parameter_name;
        delete window._recorder_showPanel;
        delete window._recorder_hidePanel;
        delete window._recorder_showParamUI;
        delete window._recorder_removePanel;
    };

    return true; // Indicate script injection success
}
"""

REMOVE_CLICK_LISTENER_JS = """
() => {
  let removed = false;
  // Remove listener
  if (window._recorderClickListener) {
    document.body.removeEventListener('click', window._recorderClickListener, { capture: true });
    delete window._recorderClickListener;
    console.log('[Recorder Listener] Listener explicitly removed.');
    removed = true;
  } else {
    console.log('[Recorder Listener] No active listener found to remove.');
  }
  // Clean up global variable
  if (window._recorder_override_selector !== undefined) {
      delete window._recorder_override_selector;
      console.log('[Recorder Listener] Override selector variable cleaned up.');
  }
  return removed;
}
"""


class BrowserController:
    """Handles Playwright browser automation tasks, including console message capture."""

    def __init__(self, headless=True, viewport_size=None):
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: Optional[Any] = None # Keep context reference
        self.page: Page | None = None
        self.headless = headless
        self.default_navigation_timeout = 9000
        self.default_action_timeout = 9000
        self._dom_service: Optional[DomService] = None
        self.console_messages: List[Dict[str, Any]] = [] # <-- Add list to store messages
        self._recorder_ui_injected = False # Track if UI script is injected
        self._panel_interaction_lock = threading.Lock() # Prevent race conditions waiting for panel
        self.viewport_size = viewport_size
        logger.info(f"BrowserController initialized (headless={headless}).")

    # inject ui panel onto the browser
    def inject_recorder_ui_scripts(self):
        """Injects the JS functions for the recorder UI panel."""
        if self.headless: return # No UI in headless
        if not self.page:
            logger.error("Page not initialized. Cannot inject recorder UI.")
            return False
        if self._recorder_ui_injected:
            logger.debug("Recorder UI scripts already injected.")
            return True
        try:
            self.page.evaluate(RECORDER_PANEL_JS)
            self._recorder_ui_injected = True
            logger.info("Recorder UI panel JavaScript injected successfully.")
            return True
        except Exception as e:
            logger.error(f"Failed to inject recorder UI panel JS: {e}", exc_info=True)
            return False
        
    def show_verification_review_panel(self, planned_desc: str, verification_result: Dict[str, Any]):
        """Shows the panel for reviewing AI verification results."""
        if self.headless or not self.page: return
        try:
            # Extract data needed by the JS function
            args = {
                "plannedDesc": planned_desc,
                "aiVerified": verification_result.get('verified', False),
                "aiReasoning": verification_result.get('reasoning', 'N/A'),
                "assertionType": verification_result.get('assertion_type'),
                "parameters": verification_result.get('parameters', {}),
                "selector": verification_result.get('verification_selector') # Use the final selector
            }

            js_script = f"""
            (args) => {{
                ({RECORDER_PANEL_JS})(); // Ensure functions are defined
                if (window._recorder_showVerificationReviewPanel) {{
                    window._recorder_showVerificationReviewPanel(args);
                }} else {{ console.error('Verification review panel function not defined!'); }}
            }}"""
            self.page.evaluate(js_script, args)
        except Exception as e:
            logger.error(f"Failed to show verification review panel: {e}", exc_info=True)
    
    def show_assertion_target_panel(self, planned_desc: str, suggested_selector: Optional[str]):
        """Shows the panel for confirming/overriding the assertion target."""
        if self.headless or not self.page: return
        try:
            js_script = f"""
            (args) => {{
                ({RECORDER_PANEL_JS})(); // Ensure functions are defined
                if (window._recorder_showAssertionTargetPanel) {{
                    window._recorder_showAssertionTargetPanel(args.plannedDesc, args.suggestedSelector);
                }} else {{ console.error('Assertion target panel function not defined!'); }}
            }}"""
            self.page.evaluate(js_script, {"plannedDesc": planned_desc, "suggestedSelector": suggested_selector})
        except Exception as e:
            logger.error(f"Failed to show assertion target panel: {e}", exc_info=True)

    def show_assertion_type_panel(self, target_selector: str):
        """Shows the panel for selecting the assertion type."""
        if self.headless or not self.page: return
        try:
            js_script = f"""
            (args) => {{
                ({RECORDER_PANEL_JS})(); // Ensure functions are defined
                if (window._recorder_showAssertionTypePanel) {{
                    window._recorder_showAssertionTypePanel(args.targetSelector);
                }} else {{ console.error('Assertion type panel function not defined!'); }}
            }}"""
            self.page.evaluate(js_script, {"targetSelector": target_selector})
        except Exception as e:
            logger.error(f"Failed to show assertion type panel: {e}", exc_info=True)

    def show_assertion_params_panel(self, target_selector: str, assertion_type: str, param_labels: List[str]):
        """Shows the panel for entering assertion parameters."""
        if self.headless or not self.page: return
        try:
            js_script = f"""
            (args) => {{
                ({RECORDER_PANEL_JS})(); // Ensure functions are defined
                if (window._recorder_showAssertionParamsPanel) {{
                    window._recorder_showAssertionParamsPanel(args.targetSelector, args.assertionType, args.paramLabels);
                }} else {{ console.error('Assertion params panel function not defined!'); }}
            }}"""
            self.page.evaluate(js_script, {
                "targetSelector": target_selector,
                "assertionType": assertion_type,
                "paramLabels": param_labels
            })
        except Exception as e:
            logger.error(f"Failed to show assertion params panel: {e}", exc_info=True)

    def get_assertion_parameters_from_panel(self, count: int) -> Optional[Dict[str, str]]:
        """Retrieves the parameter values entered in the assertion panel."""
        if self.headless or not self.page: return None
        try:
            params = self.page.evaluate("window._recorder_getAssertionParams ? window._recorder_getAssertionParams(count) : null", {"count": count})
            return params
        except Exception as e:
            logger.error(f"Failed to get assertion parameters from panel: {e}")
            return None

    def show_recorder_panel(self, step_description: str, suggestion_text: str):
        """Shows the recorder UI panel with step info."""
        if self.headless or not self.page:
            logger.warning("Cannot show recorder panel (headless or no page).")
            return
        try:
            # Evaluate a script that FIRST defines the functions, THEN calls showPanel
            js_script = f"""
            (args) => {{
                // Ensure panel functions are defined (runs the definitions)
                ({RECORDER_PANEL_JS})();

                // Now call the show function
                if (window._recorder_showPanel) {{
                    window._recorder_showPanel(args.stepDescription, args.suggestionText);
                }} else {{
                     console.error('[Recorder Panel] _recorder_showPanel function is still not defined after injection attempt!');
                }}
            }}
            """
            self.page.evaluate(js_script, {"stepDescription": step_description, "suggestionText": suggestion_text})
        except Exception as e:
            logger.error(f"Failed to show recorder panel: {e}", exc_info=True) # Log full trace for debugging

    def hide_recorder_panel(self):
        """Hides the recorder UI panel if it exists."""
        if self.headless or not self.page: return
        try:
            # Check if function exists before calling
            self.page.evaluate("if (window._recorder_hidePanel) window._recorder_hidePanel()")
        except Exception as e:
            logger.warning(f"Failed to hide recorder panel (might be removed or page navigated): {e}")

    def remove_recorder_panel(self):
        """Removes the recorder UI panel from the DOM if it exists."""
        if self.headless or not self.page: return
        try:
            # Check if function exists before calling
            self.page.evaluate("if (window._recorder_removePanel) window._recorder_removePanel()")
        except Exception as e:
            logger.warning(f"Failed to remove recorder panel (might be removed or page navigated): {e}")

    def prompt_parameterization_in_panel(self, default_value: str) -> bool:
        """Shows the parameterization input field, ensuring functions are defined."""
        if self.headless or not self.page: return False
        try:
            # Combine definition and call again
            js_script = f"""
            (args) => {{
                 // Ensure panel functions are defined
                ({RECORDER_PANEL_JS})();

                // Now call the show param UI function
                if (window._recorder_showParamUI) {{
                    return window._recorder_showParamUI(args.defaultValue);
                }} else {{
                     console.error('[Recorder Panel] _recorder_showParamUI function is still not defined!');
                     return false;
                }}
            }}
            """
            success = self.page.evaluate(js_script, {"defaultValue": default_value})
            return success if success is True else False # Ensure boolean return
        except Exception as e:
            logger.error(f"Failed to show parameterization UI in panel: {e}")
            return False

    def wait_for_panel_interaction(self, timeout_seconds: float) -> Optional[str]:
        """
        Waits for the user to click a button on the recorder panel.
        Returns the choice ('accept', 'skip', 'abort', 'parameterized') or None on timeout.
        """
        if self.headless or not self.page or not self._recorder_ui_injected: return None

        with self._panel_interaction_lock: # Prevent concurrent waits if called rapidly
            js_condition = "() => window._recorder_user_choice !== undefined"
            timeout_ms = timeout_seconds * 1000
            user_choice = None

            logger.info(f"Waiting up to {timeout_seconds}s for user interaction via UI panel...")

            try:
                # Ensure the flag is initially undefined before waiting
                self.page.evaluate("window._recorder_user_choice = undefined")

                self.page.wait_for_function(js_condition, timeout=timeout_ms)

                # If wait succeeds, get the choice
                user_choice = self.page.evaluate("window._recorder_user_choice")
                logger.info(f"User interaction detected via panel: '{user_choice}'")

            except PlaywrightTimeoutError:
                logger.warning("Timeout reached waiting for panel interaction.")
                user_choice = None # Timeout occurred
            except Exception as e:
                logger.error(f"Error during page.wait_for_function for panel interaction: {e}", exc_info=True)
                user_choice = None # Treat other errors as timeout/failure
            finally:
                # Reset the flag *immediately after reading or timeout* for the next wait
                 try:
                     self.page.evaluate("window._recorder_user_choice = undefined")
                 except Exception:
                      logger.warning("Could not reset panel choice flag after interaction/timeout.")

        return user_choice

    def get_parameterization_result(self) -> Optional[str]:
         """Retrieves the parameter name entered in the panel. Call after wait_for_panel_interaction returns 'parameterized'."""
         if self.headless or not self.page or not self._recorder_ui_injected: return None
         try:
             param_name = self.page.evaluate("window._recorder_parameter_name")
             # Reset the flag after reading
             self.page.evaluate("window._recorder_parameter_name = undefined")
             logger.debug(f"Retrieved parameter name from panel: {param_name}")
             return param_name # Can be string or null
         except Exception as e:
             logger.error(f"Failed to get parameter name from panel: {e}")
             return None


    # Recorder Methods begin =============
    def setup_click_listener(self) -> bool:
        """Injects JS to listen for the next user click and report the selector."""
        if self.headless:
             logger.error("Cannot set up click listener in headless mode.")
             return False
        if not self.page:
            logger.error("Page not initialized. Cannot set up click listener.")
            return False
        try:
            # Inject and run the listener setup JS
            # It now resets the flag internally before adding the listener
            self.page.evaluate(CLICK_LISTENER_JS)
            logger.info("JavaScript click listener attached (using pre-exposed callback).")
            return True

        except Exception as e:
            logger.error(f"Failed to set up recorder click listener: {e}", exc_info=True)
            return False

    def remove_click_listener(self) -> bool:
        """Removes the injected JS click listener."""
        if self.headless: return True # Nothing to remove
        if not self.page:
            logger.warning("Page not initialized. Cannot remove click listener.")
            return False
        try:
            removed = self.page.evaluate(REMOVE_CLICK_LISTENER_JS)

            return removed
        except Exception as e:
            logger.error(f"Failed to remove recorder click listener: {e}", exc_info=True)
            return False

    def wait_for_user_click_or_timeout(self, timeout_seconds: float) -> Optional[str]:
        """
        Waits for the user to click (triggering the callback) or for the timeout.
        Returns the selector if clicked, None otherwise.
        MUST be called after setup_click_listener.
        """
        if self.headless: return None
        if not self.page:
             logger.error("Page not initialized. Cannot wait for click function.")
             return None

        selector_result = None
        js_condition = "() => window._recorder_override_selector !== undefined"
        timeout_ms = timeout_seconds * 1000

        logger.info(f"Waiting up to {timeout_seconds}s for user click (checking JS flag)...")

        try:
            # Wait for the JS condition to become true
            self.page.wait_for_function(js_condition, timeout=timeout_ms)

            # If wait_for_function completes without timeout, the flag was set
            logger.info("User click detected (JS flag set)!")
            # Retrieve the value set by the click handler
            selector_result = self.page.evaluate("window._recorder_override_selector")
            logger.debug(f"Retrieved selector from JS flag: {selector_result}")

        except PlaywrightTimeoutError:
            logger.info("Timeout reached waiting for user click (JS flag not set).")
            selector_result = None # Timeout occurred
        except Exception as e:
             logger.error(f"Error during page.wait_for_function: {e}", exc_info=True)
             selector_result = None # Treat other errors as timeout/failure

        finally:
             # Clean up the JS listener and the flag regardless of outcome
             self.remove_click_listener()

        return selector_result

    # Recorder methods end

    # Highlighting elements
    def highlight_element(self, selector: str, index: int, color: str = "#FF0000", text: Optional[str] = None, node_xpath: Optional[str] = None):
        """Highlights an element using a specific selector and index label."""
        if self.headless or not self.page: return
        try:
            self.page.evaluate("""
                (args) => {
                    const { selector, index, color, text, node_xpath } = args;
                    const HIGHLIGHT_CONTAINER_ID = "bw-highlight-container"; // Unique ID

                    let container = document.getElementById(HIGHLIGHT_CONTAINER_ID);
                    if (!container) {
                        container = document.createElement("div");
                        container.id = HIGHLIGHT_CONTAINER_ID;
                        container.style.position = "fixed";
                        container.style.pointerEvents = "none";
                        container.style.top = "0";
                        container.style.left = "0";
                        container.style.width = "0"; // Occupy no space
                        container.style.height = "0";
                        container.style.zIndex = "2147483646"; // Below listener potentially
                        document.body.appendChild(container);
                    }

                    let element = null;
                    try {
                        element = document.querySelector(selector);
                    } catch (e) {
                        console.warn(`[Highlighter] querySelector failed for '${selector}': ${e.message}.`);
                        element = null; // Ensure element is null if querySelector fails
                    }

                    // --- Fallback to XPath if CSS failed AND xpath is available ---
                    if (!element && node_xpath) {
                        console.log(`[Highlighter] Falling back to XPath: ${node_xpath}`);
                        try {
                            element = document.evaluate(
                                node_xpath,
                                document,
                                null,
                                XPathResult.FIRST_ORDERED_NODE_TYPE,
                                null
                            ).singleNodeValue;
                        } catch (e) {
                            console.error(`[Highlighter] XPath evaluation failed for '${node_xpath}': ${e.message}`);
                            element = null;
                        }
                    }
                    // ------------------------------------------------------------

                    if (!element) {
                        console.warn(`[Highlighter] Element not found using selector '${selector}' or XPath '${node_xpath}'. Cannot highlight.`);
                        return;
                    }

                    const rect = element.getBoundingClientRect();
                    if (!rect || rect.width === 0 || rect.height === 0) return; // Don't highlight non-rendered

                    const overlay = document.createElement("div");
                    overlay.style.position = "fixed";
                    overlay.style.border = `2px solid ${color}`;
                    overlay.style.backgroundColor = color + '1A'; // 10% opacity
                    overlay.style.pointerEvents = "none";
                    overlay.style.boxSizing = "border-box";
                    overlay.style.top = `${rect.top}px`;
                    overlay.style.left = `${rect.left}px`;
                    overlay.style.width = `${rect.width}px`;
                    overlay.style.height = `${rect.height}px`;
                    overlay.style.zIndex = "2147483646";
                    overlay.setAttribute('data-highlight-selector', selector); // Mark for cleanup
                    container.appendChild(overlay);

                    const label = document.createElement("div");
                    const labelText = text ? `${index}: ${text}` : `${index}`;
                    label.style.position = "fixed";
                    label.style.background = color;
                    label.style.color = "white";
                    label.style.padding = "1px 4px";
                    label.style.borderRadius = "4px";
                    label.style.fontSize = "10px";
                    label.style.fontWeight = "bold";
                    label.style.zIndex = "2147483647";
                    label.textContent = labelText;
                    label.setAttribute('data-highlight-selector', selector); // Mark for cleanup

                    // Position label top-left, slightly offset
                    let labelTop = rect.top - 18;
                    let labelLeft = rect.left;
                     // Adjust if label would go off-screen top
                    if (labelTop < 0) labelTop = rect.top + 2;

                    label.style.top = `${labelTop}px`;
                    label.style.left = `${labelLeft}px`;
                    container.appendChild(label);
                }
            """, {"selector": selector, "index": index, "color": color, "text": text, "node_xpath": node_xpath})
        except Exception as e:
            logger.warning(f"Failed to highlight element '{selector}': {e}")

    def clear_highlights(self):
        """Removes all highlight overlays and labels added by highlight_element."""
        if self.headless or not self.page: return
        try:
            self.page.evaluate("""
                () => {
                    const container = document.getElementById("bw-highlight-container");
                    if (container) {
                        container.innerHTML = ''; // Clear contents efficiently
                    }
                }
            """)
            # logger.debug("Cleared highlights.")
        except Exception as e:
            logger.warning(f"Could not clear highlights: {e}")

    def get_browser_version(self) -> str:
        if not self.browser:
            return "Unknown"
        try:
            # Browser version might be available directly
            return f"{self.browser.browser_type.name} {self.browser.version}"
        except Exception:
            logger.warning("Could not retrieve exact browser version.")
            return self.browser.browser_type.name if self.browser else "Unknown"

    def get_os_info(self) -> str:
        try:
            return f"{platform.system()} {platform.release()}"
        except Exception:
            logger.warning("Could not retrieve OS information.")
            return "Unknown"

    def get_viewport_size(self) -> Optional[Dict[str, int]]:
         if not self.page:
              return None
         try:
              return self.page.viewport_size # Returns {'width': W, 'height': H} or None
         except Exception:
             logger.warning("Could not retrieve viewport size.")
             return None

    def _handle_console_message(self, message: ConsoleMessage):
        """Callback function to handle console messages."""
        msg_type = message.type
        msg_text = message.text
        timestamp = time.time()
        log_entry = {
            "timestamp": timestamp,
            "type": msg_type,
            "text": msg_text,
            # Optional: Add location if needed, but can be verbose
            # "location": message.location()
        }
        self.console_messages.append(log_entry)
        # Optional: Log immediately to agent's log file for real-time debugging
        log_level = logging.WARNING if msg_type in ['error', 'warning'] else logging.DEBUG
        logger.log(log_level, f"[CONSOLE.{msg_type.upper()}] {msg_text}")

    def check(self, selector: str): 
        """Checks a checkbox or radio button."""
        if not self.page:
            raise PlaywrightError("Browser not started.")
        try:
            logger.info(f"Attempting to check element: {selector}")
            locator = self.page.locator(selector).first
            # check() includes actionability checks (visible, enabled)
            locator.check(timeout=self.default_action_timeout)
            logger.info(f"Checked element: {selector}")
            self._human_like_delay(0.2, 0.5) # Small delay after checking
        except PlaywrightTimeoutError as e:
            logger.error(f"Timeout ({self.default_action_timeout}ms) waiting for element '{selector}' to be actionable for check.")
            # Add screenshot on failure
            screenshot_path = f"output/check_timeout_{selector.replace(' ','_').replace(':','_').replace('>','_')[:30]}_{int(time.time())}.png"
            self.save_screenshot(screenshot_path)
            logger.error(f"Saved screenshot on check timeout to: {screenshot_path}")
            raise PlaywrightTimeoutError(f"Timeout trying to check element: '{selector}'. Check visibility and enabled state. Screenshot: {screenshot_path}") from e
        except PlaywrightError as e:
            logger.error(f"PlaywrightError checking element '{selector}': {e}")
            raise PlaywrightError(f"Failed to check element '{selector}': {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error checking '{selector}': {e}", exc_info=True)
            raise PlaywrightError(f"Unexpected error checking element '{selector}': {e}") from e

    def uncheck(self, selector: str):
        """Unchecks a checkbox."""
        if not self.page:
            raise PlaywrightError("Browser not started.")
        try:
            logger.info(f"Attempting to uncheck element: {selector}")
            locator = self.page.locator(selector).first
            # uncheck() includes actionability checks
            locator.uncheck(timeout=self.default_action_timeout)
            logger.info(f"Unchecked element: {selector}")
            self._human_like_delay(0.2, 0.5) # Small delay
        except PlaywrightTimeoutError as e:
            logger.error(f"Timeout ({self.default_action_timeout}ms) waiting for element '{selector}' to be actionable for uncheck.")
            screenshot_path = f"output/uncheck_timeout_{selector.replace(' ','_').replace(':','_').replace('>','_')[:30]}_{int(time.time())}.png"
            self.save_screenshot(screenshot_path)
            logger.error(f"Saved screenshot on uncheck timeout to: {screenshot_path}")
            raise PlaywrightTimeoutError(f"Timeout trying to uncheck element: '{selector}'. Screenshot: {screenshot_path}") from e
        except PlaywrightError as e:
            logger.error(f"PlaywrightError unchecking element '{selector}': {e}")
            raise PlaywrightError(f"Failed to uncheck element '{selector}': {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error unchecking '{selector}': {e}", exc_info=True)
            raise PlaywrightError(f"Unexpected error unchecking element '{selector}': {e}") from e
        
    def start(self):
        """Starts Playwright, launches browser, creates context/page, and attaches console listener."""
        try:
            logger.info("Starting Playwright...")
            self.playwright = sync_playwright().start()
            # Consider adding args for anti-detection if needed:
            browser_args = ['--disable-blink-features=AutomationControlled']
            self.browser = self.playwright.chromium.launch(headless=self.headless, args=browser_args)
            # self.browser = self.playwright.chromium.launch(headless=self.headless)

            self.context = self.browser.new_context(
                 user_agent=self._get_random_user_agent(),
                 viewport=self._get_random_viewport(),
                 ignore_https_errors=True,
                 java_script_enabled=True,
                 extra_http_headers=COMMON_HEADERS,
            )
            self.context.set_default_navigation_timeout(self.default_navigation_timeout)
            self.context.set_default_timeout(self.default_action_timeout)
            self.context.add_init_script(HIDE_WEBDRIVER_SCRIPT)

            self.page = self.context.new_page()

            # Initialize DomService with the created page
            self._dom_service = DomService(self.page) # Instantiate here
            
            # --- Attach Console Listener ---
            self.page.on('console', self._handle_console_message)
            logger.info("Attached console message listener to the page.")

            self.inject_recorder_ui_scripts() # inject recorder ui
            
            # -----------------------------
            logger.info("Browser context and page created.")

        except Exception as e:
            logger.error(f"Failed to start Playwright or launch browser: {e}", exc_info=True)
            self.close() # Ensure cleanup on failure
            raise
    
    def get_console_messages(self) -> List[Dict[str, Any]]:
        """Returns a copy of the captured console messages."""
        return list(self.console_messages) # Return a copy

    def clear_console_messages(self):
        """Clears the stored console messages."""
        logger.debug("Clearing captured console messages.")
        self.console_messages = []


    def _get_random_user_agent(self):
        """Provides a random choice from a list of common user agents."""
        user_agents = [
            # Chrome on Windows
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36',
             # Chrome on Mac
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
            # Firefox on Windows
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0',
            # Add more variations if desired (Edge, Safari etc.)
        ]
        return random.choice(user_agents)

    def _get_random_viewport(self):
        """Provides a slightly randomized common viewport size."""
        common_sizes = [
            # {'width': 1280, 'height': 720},
            # {'width': 1366, 'height': 768},
            {'width': 800, 'height': 600},
            # {'width': 1536, 'height': 864},
        ]
        base = random.choice(common_sizes)
        # Add small random offset
        if not self.viewport_size:
            base['width'] += random.randint(-10, 10)
            base['height'] += random.randint(-5, 5)
        else:
            base = self.viewport_size
        return base

    def close(self):
        """Closes the browser and stops Playwright."""
        self.remove_recorder_panel()
        self.remove_click_listener() 
        try:
            self._dom_service = None
            if self.page and not self.page.is_closed():
                # logger.debug("Closing page...") # Added for clarity
                self.page.close()
                # logger.debug("Page closed.")
            else:
                logger.debug("Page already closed or not initialized.")
            if self.context:
                 self.context.close()
                 logger.info("Browser context closed.")
            if self.browser:
                self.browser.close()
                logger.info("Browser closed.")
            if self.playwright:
                self.playwright.stop()
                logger.info("Playwright stopped.")
        except Exception as e:
            logger.error(f"Error during browser/Playwright cleanup: {e}", exc_info=True)
        finally:
            self.page = None
            self.context = None
            self.browser = None
            self.playwright = None
            self.console_messages = [] # Clear messages on final close
            self._recorder_ui_injected = False

    def validate_assertion(self, assertion_type: str, selector: str, params: Dict[str, Any], timeout_ms: int = 3000) -> Tuple[bool, Optional[str]]:
        """
        Performs a quick Playwright check to validate a proposed assertion. 

        Args:
            assertion_type: The type of assertion (e.g., 'assert_visible').
            selector: The CSS selector for the target element.
            params: Dictionary of parameters for the assertion (e.g., expected_text).
            timeout_ms: Short timeout for the validation check.

        Returns:
            Tuple (bool, Optional[str]): (True, None) if validation passes,
                                         (False, error_message) if validation fails.
        """
        if not self.page:
            return False, "Page not initialized."
        if not selector:
            # Assertions like 'assert_llm_verification' might not have a selector
            if assertion_type == 'assert_llm_verification':
                logger.info("Skipping validation for 'assert_llm_verification' as it relies on external LLM check.")
                return True, None
            return False, "Selector is required for validation."
        if not assertion_type:
            return False, "Assertion type is required for validation."

        logger.info(f"Validating assertion: {assertion_type} on '{selector}' with params {params} (timeout: {timeout_ms}ms)")
        try:
            locator = self._get_locator(selector) # Use helper to handle xpath/css

            # Use Playwright's expect() for efficient checks
            if assertion_type == 'assert_visible':
                expect(locator).to_be_visible(timeout=timeout_ms)
            elif assertion_type == 'assert_hidden':
                expect(locator).to_be_hidden(timeout=timeout_ms)
            elif assertion_type == 'assert_text_equals':
                expected_text = params.get('expected_text')
                if expected_text is None: return False, "Missing 'expected_text' parameter for assert_text_equals"
                expect(locator).to_have_text(expected_text, timeout=timeout_ms)
            elif assertion_type == 'assert_text_contains':
                expected_text = params.get('expected_text')
                if expected_text is None: return False, "Missing 'expected_text' parameter for assert_text_contains"
                expect(locator).to_contain_text(expected_text, timeout=timeout_ms)
            elif assertion_type == 'assert_attribute_equals':
                attr_name = params.get('attribute_name')
                expected_value = params.get('expected_value')
                if not attr_name: return False, "Missing 'attribute_name' parameter"
                # Note: Playwright's to_have_attribute handles presence and value check
                expect(locator).to_have_attribute(attr_name, expected_value if expected_value is not None else "", timeout=timeout_ms) # Check empty string if value is None/missing? Or require value? Let's require non-None value.
                # if expected_value is None: return False, "Missing 'expected_value' parameter" # Stricter check
                # expect(locator).to_have_attribute(attr_name, expected_value, timeout=timeout_ms)
            elif assertion_type == 'assert_element_count':
                expected_count = params.get('expected_count')
                if expected_count is None: return False, "Missing 'expected_count' parameter"
                # Re-evaluate locator to get all matches for count
                all_matches_locator = self.page.locator(selector)
                expect(all_matches_locator).to_have_count(expected_count, timeout=timeout_ms)
            elif assertion_type == 'assert_checked':
                expect(locator).to_be_checked(timeout=timeout_ms)
            elif assertion_type == 'assert_not_checked':
                 # Use expect(...).not_to_be_checked()
                 expect(locator).not_to_be_checked(timeout=timeout_ms)
            elif assertion_type == 'assert_enabled':
                 expect(locator).to_be_enabled(timeout=timeout_ms)
            elif assertion_type == 'assert_disabled':
                 expect(locator).to_be_disabled(timeout=timeout_ms)
            elif assertion_type == 'assert_llm_verification':
                 logger.info("Skipping Playwright validation for 'assert_llm_verification'.")
                 # This assertion type is validated externally by the LLM during execution.
                 pass # Treat as passed for this quick check
            else:
                return False, f"Unsupported assertion type for validation: {assertion_type}"

            # If no exception was raised by expect()
            logger.info(f"Validation successful for {assertion_type} on '{selector}'.")
            return True, None

        except PlaywrightTimeoutError as e:
            err_msg = f"Validation failed for {assertion_type} on '{selector}': Timeout ({timeout_ms}ms) - {str(e).splitlines()[0]}"
            logger.warning(err_msg)
            return False, err_msg
        except AssertionError as e: # Catch expect() assertion failures
            err_msg = f"Validation failed for {assertion_type} on '{selector}': Condition not met - {str(e).splitlines()[0]}"
            logger.warning(err_msg)
            return False, err_msg
        except PlaywrightError as e:
            err_msg = f"Validation failed for {assertion_type} on '{selector}': PlaywrightError - {str(e).splitlines()[0]}"
            logger.warning(err_msg)
            return False, err_msg
        except Exception as e:
            err_msg = f"Unexpected error during validation for {assertion_type} on '{selector}': {type(e).__name__} - {e}"
            logger.error(err_msg, exc_info=True)
            return False, err_msg

    def get_structured_dom(self, highlight_all_clickable_elements: bool = True, viewport_expansion: int = 0) -> Optional[DOMState]:
        """
        Uses DomService to get a structured representation of the interactive DOM elements.

        Args:
            highlight_all_clickable_elements: Whether to visually highlight elements in the browser.
            viewport_expansion: Pixel value to expand the viewport for element detection (0=viewport only, -1=all).

        Returns:
            A DOMState object containing the element tree and selector map, or None on error.
        """
        highlight_all_clickable_elements = False # SETTING TO FALSE TO AVOID CONFUSION WITH NEXT ACTION HIGHLIGHT
        
        if not self.page:
            logger.error("Browser/Page not initialized or DomService unavailable.")
            return None
        if not self._dom_service:
            self._dom_service = DomService(self.page)


        # --- RECORDER MODE: Never highlight via JS during DOM build ---
        # Highlighting is done separately by BrowserController.highlight_element
        if self.headless == False: # Assume non-headless is recorder mode context
             highlight_all_clickable_elements = False
        # --- END RECORDER MODE ---
        
        if not self._dom_service:
            logger.error("DomService unavailable.")
            return None

        try:
            logger.info(f"Requesting structured DOM (highlight={highlight_all_clickable_elements}, expansion={viewport_expansion})...")
            start_time = time.time()
            dom_state = self._dom_service.get_clickable_elements(
                highlight_elements=highlight_all_clickable_elements,
                focus_element=-1, # Not focusing on a specific element for now
                viewport_expansion=viewport_expansion
            )
            end_time = time.time()
            logger.info(f"Structured DOM retrieved in {end_time - start_time:.2f}s. Found {len(dom_state.selector_map)} interactive elements.")
            # Generate selectors immediately for recorder use
            if dom_state and dom_state.selector_map:
                for node in dom_state.selector_map.values():
                     if not node.css_selector:
                           node.css_selector = self.get_selector_for_node(node)
            return dom_state
        
        except Exception as e:
            logger.error(f"Error getting structured DOM: {type(e).__name__}: {e}", exc_info=True)
            return None
    
    def get_selector_for_node(self, node: DOMElementNode) -> Optional[str]:
        """Generates a robust CSS selector for a given DOMElementNode."""
        if not node: return None
        try:
            # Use the static method from DomService
            return DomService._enhanced_css_selector_for_element(node)
        except Exception as e:
             logger.error(f"Error generating selector for node {node.xpath}: {e}", exc_info=True)
             return node.xpath # Fallback to xpath
    
    def goto(self, url: str):
        """Navigates the page to a specific URL."""
        if not self.page:
            raise PlaywrightError("Browser not started. Call start() first.")
        try:
            logger.info(f"Navigating to URL: {url}")
            # Use default navigation timeout set in context
            response = self.page.goto(url, wait_until='domcontentloaded', timeout=self.default_navigation_timeout) # 'load' or 'networkidle' might be better sometimes
            # Add a small stable delay after load
            time.sleep(2)
            status = response.status if response else 'unknown'
            logger.info(f"Navigation to {url} finished with status: {status}.")
            if response and not response.ok:
                 logger.warning(f"Navigation to {url} resulted in non-OK status: {status}")
                 # Optionally raise an error here if needed
        except PlaywrightTimeoutError as e:
            logger.error(f"Timeout navigating to {url}: {e}")
            # Re-raise with a clearer message for the agent
            raise PlaywrightTimeoutError(f"Timeout loading page {url}. The page might be too slow or unresponsive.") from e
        except PlaywrightError as e: # Catch broader Playwright errors
            logger.error(f"Playwright error navigating to {url}: {e}")
            raise PlaywrightError(f"Error navigating to {url}: {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error navigating to {url}: {e}", exc_info=True)
            raise # Re-raise for the agent to handle

    def get_html(self) -> str:
        """Returns the full HTML content of the current page."""
        if not self.page:
            logger.error("Cannot get HTML, browser not started.")
            raise Exception("Browser not started.")
        try:
            html = self.page.content()
            logger.info("Retrieved page HTML content.")
            # logger.debug(f"HTML content length: {len(html)}")
            return html
        except Exception as e:
            logger.error(f"Error getting HTML content: {e}", exc_info=True)
            return f"Error retrieving HTML: {e}"

    def get_current_url(self) -> str:
        """Returns the current URL of the page."""
        if not self.page:
            return "Error: Browser not started."
        try:
            return self.page.url
        except Exception as e:
            logger.error(f"Error getting current URL: {e}", exc_info=True)
            return f"Error retrieving URL: {e}"


    def take_screenshot(self) -> bytes | None:
        """Takes a screenshot of the current page and returns bytes."""
        if not self.page:
            logger.error("Cannot take screenshot, browser not started.")
            return None
        try:
            screenshot_bytes = self.page.screenshot()
            logger.info("Screenshot taken (bytes).")
            return screenshot_bytes
        except Exception as e:
            logger.error(f"Error taking screenshot: {e}", exc_info=True)
            return None
        
    def save_screenshot(self, file_path: str) -> bool:
        """Takes a screenshot and saves it to the specified file path."""
        if not self.page:
            logger.error(f"Cannot save screenshot to {file_path}, browser not started.")
            return False
        try:
            # Ensure directory exists
            abs_file_path = os.path.abspath(file_path)
            os.makedirs(os.path.dirname(abs_file_path), exist_ok=True)

            self.page.screenshot(path=abs_file_path)
            logger.info(f"Screenshot saved to: {abs_file_path}")
            return True
        except Exception as e:
            logger.error(f"Error saving screenshot to {file_path}: {e}", exc_info=True)
            return False

    def _find_element(self, selector: str, timeout=None) -> Optional[Locator]:
        """Finds the first element matching the selector."""
        if not self.page:
             raise PlaywrightError("Browser not started.")
        effective_timeout = timeout if timeout is not None else self.default_action_timeout
        logger.debug(f"Attempting to find element: '{selector}' (timeout: {effective_timeout}ms)")
        try:
             # Use locator().first to explicitly target the first match
             element = self.page.locator(selector).first
             # Brief wait for attached state, primary checks in actions
             element.wait_for(state='attached', timeout=effective_timeout * 0.5)
             # Scroll into view if needed
             try:
                  element.scroll_into_view_if_needed(timeout=effective_timeout * 0.25)
                  time.sleep(0.1)
             except Exception as scroll_e:
                  logger.warning(f"Non-critical: Could not scroll element {selector} into view. Error: {scroll_e}")
             logger.debug(f"Element found and attached: '{selector}'")
             return element
        except PlaywrightTimeoutError:
             # Don't log as error here, actions will report failure if needed
             logger.debug(f"Timeout ({effective_timeout}ms) waiting for element state 'attached' or scrolling: '{selector}'.")
             return None
        except PlaywrightError as e:
            logger.error(f"PlaywrightError finding element '{selector}': {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error finding element '{selector}': {e}", exc_info=True)
            return None
    
    def click(self, selector: str):
        """Clicks an element, relying on Playwright's built-in actionability checks."""
        if not self.page:
            raise PlaywrightError("Browser not started.")
        try:
            logger.info(f"Attempting to click element: {selector}")
            locator = self.page.locator(selector).first #
            logger.debug(f"Executing click on locator for '{selector}' (with built-in checks)...")
            click_delay = random.uniform(50, 150)

            # Optional: Try hover first
            try:
                locator.hover(timeout=3000) # Short timeout for hover
                self._human_like_delay(0.1, 0.3)
            except Exception:
                logger.debug(f"Hover failed or timed out for {selector}, proceeding with click.")

            # Perform the click with its own timeout
            locator.click(delay=click_delay, timeout=self.default_action_timeout)
            logger.info(f"Clicked element: {selector}")
            self._human_like_delay(0.5, 1.5) # Post-click delay

        except PlaywrightTimeoutError as e:
            # Timeout occurred *during* the click action's internal waits
            logger.error(f"Timeout ({self.default_action_timeout}ms) waiting for element '{selector}' to be actionable for click. Element might be obscured, disabled, unstable, or not found.")
            # Add more context to the error message
            screenshot_path = f"output/click_timeout_{selector.replace(' ','_').replace(':','_').replace('>','_')[:30]}_{int(time.time())}.png"
            self.save_screenshot(screenshot_path)
            logger.error(f"Saved screenshot on click timeout to: {screenshot_path}")
            raise PlaywrightTimeoutError(f"Timeout trying to click element: '{selector}'. Check visibility, interactability, and selector correctness. Screenshot saved to {screenshot_path}") from e
        except PlaywrightError as e:
             # Other errors during click
             logger.error(f"PlaywrightError clicking element '{selector}': {e}")
             raise PlaywrightError(f"Failed to click element '{selector}': {e}") from e
        except Exception as e:
             logger.error(f"Unexpected error clicking '{selector}': {e}", exc_info=True)
             raise PlaywrightError(f"Unexpected error clicking element '{selector}': {e}") from e

    def type(self, selector: str, text: str):
        """
        Inputs text into an element, prioritizing the robust `fill` method.
        Includes fallback to `type`.
        """
        if not self.page:
            raise PlaywrightError("Browser not started.")
        try:
            logger.info(f"Attempting to input text '{text[:30]}...' into element: {selector}")
            locator = self.page.locator(selector).first

            # --- Strategy 1: Use fill() ---
            # fill() clears the field first and inputs text.
            # It performs actionability checks (visible, enabled, editable etc.)
            
            logger.debug(f"Trying to 'fill' locator for '{selector}' (includes actionability checks)...")
            try:
                if not self.headless: time.sleep(0.2)
                locator.fill(text, timeout=self.default_action_timeout) # Use default action timeout
                logger.info(f"'fill' successful for element: {selector}")
                self._human_like_delay(0.3, 0.8) # Delay after successful input
                return # Success! Exit the method.
            except (PlaywrightTimeoutError, PlaywrightError) as fill_error:
                logger.warning(f"'fill' action failed for '{selector}': {fill_error}. Attempting fallback to 'type'.")
            
            # Proceed to fallback

            # --- Strategy 2: Fallback to type() ---
            logger.debug(f"Trying fallback 'type' for locator '{selector}'...")
            try:
                # Ensure element is clear before typing as a fallback precaution
                locator.clear(timeout=self.default_action_timeout * 0.5) # Quick clear attempt
                self._human_like_delay(0.1, 0.3)
                typing_delay_ms = random.uniform(90, 180)
                locator.type(text, delay=typing_delay_ms, timeout=self.default_action_timeout)
                logger.info(f"Fallback 'type' successful for element: {selector}")
                self._human_like_delay(0.3, 0.8)
                return # Success!
            except (PlaywrightTimeoutError, PlaywrightError) as type_error:
                 logger.error(f"Both 'fill' and fallback 'type' failed for '{selector}'. Last error ('type'): {type_error}")
                 # Raise the error from the 'type' attempt as it was the last one tried
                 screenshot_path = f"output/type_fail_{selector.replace(' ','_').replace(':','_').replace('>','_')[:30]}_{int(time.time())}.png"
                 self.save_screenshot(screenshot_path)
                 logger.error(f"Saved screenshot on type failure to: {screenshot_path}")
                 # Raise a combined error or the last one
                 raise PlaywrightError(f"Failed to input text into element '{selector}' using both fill and type. Last error: {type_error}. Screenshot: {screenshot_path}") from type_error

        # Catch errors related to finding/interacting
        except PlaywrightTimeoutError as e:
             # This might catch timeouts from clear() or the actionability checks within fill/type
             logger.error(f"Timeout ({self.default_action_timeout}ms) during input operation stages for selector: '{selector}'. Element might not become actionable.")
             screenshot_path = f"output/input_timeout_{selector.replace(' ','_').replace(':','_').replace('>','_')[:30]}_{int(time.time())}.png"
             self.save_screenshot(screenshot_path)
             logger.error(f"Saved screenshot on input timeout to: {screenshot_path}")
             raise PlaywrightTimeoutError(f"Timeout trying to input text into element: '{selector}'. Check interactability. Screenshot: {screenshot_path}") from e
        except PlaywrightError as e:
             # Covers other Playwright issues like element detached during operation
             logger.error(f"PlaywrightError inputting text into element '{selector}': {e}")
             raise PlaywrightError(f"Failed to input text into element '{selector}': {e}") from e
        except Exception as e:
             logger.error(f"Unexpected error inputting text into '{selector}': {e}", exc_info=True)
             raise PlaywrightError(f"Unexpected error inputting text into element '{selector}': {e}") from e
             
    def scroll(self, direction: str):
        """Scrolls the page up or down with a slight delay."""
        if not self.page:
            raise PlaywrightError("Browser not started.")
        try:
            scroll_amount = "window.innerHeight"
            if direction == "down":
                self.page.evaluate(f"window.scrollBy(0, {scroll_amount})")
                logger.info("Scrolled down.")
            elif direction == "up":
                self.page.evaluate(f"window.scrollBy(0, -{scroll_amount})")
                logger.info("Scrolled up.")
            else:
                logger.warning(f"Invalid scroll direction: {direction}")
                return # Don't delay for invalid direction
            self._human_like_delay(0.4, 0.8) # Delay after scrolling
        except Exception as e:
            logger.error(f"Error scrolling {direction}: {e}", exc_info=True)
            
    def extract_text(self, selector: str) -> str:
        """Extracts the text content from the first element matching the selector."""
        if not self.page:
            raise PlaywrightError("Browser not started, cannot extract text.")
        try:
            logger.info(f"Extracting text from selector: {selector}")
            locator = self.page.locator(selector).first # Get locator

            # Use text_content() which has implicit waits, but add explicit short wait for visibility first
            try:
                 # Wait for element to be at least visible, maybe attached is enough?
                 # Let's stick to visible for text extraction. Use a shorter timeout.
                 locator.wait_for(state='visible', timeout=self.default_action_timeout * 0.75)
            except PlaywrightTimeoutError:
                 # Element didn't become visible in time
                 # Check if it exists but is hidden
                 is_attached = False
                 try:
                      is_attached = locator.is_attached() # Check if it's in DOM but hidden
                 except: pass # Ignore errors here

                 if is_attached:
                      logger.warning(f"Element '{selector}' found in DOM but is not visible within timeout for text extraction.")
                      return "Error: Element found but not visible"
                 else:
                      error_msg = f"Timeout waiting for element '{selector}' to be visible for text extraction."
                      logger.error(error_msg)
                      return f"Error: {error_msg}"

            # If visible, proceed to get text
            text = locator.text_content() # Get text content
            if text is None: text = ""
            logger.info(f"Successfully extracted text from '{selector}': '{text[:100]}...'")
            return text.strip()

        except PlaywrightError as e: # Catch other Playwright errors during text_content or wait_for
            logger.error(f"PlaywrightError extracting text from '{selector}': {e}")
            return f"Error extracting text: {type(e).__name__}: {e}"
        except Exception as e:
            logger.error(f"Unexpected error extracting text from '{selector}': {e}", exc_info=True)
            return f"Error extracting text: {type(e).__name__}: {e}"


    def extract_attributes(self, selector: str, attributes: List[str]) -> Dict[str, Optional[str]]:
        """Extracts specified attributes from the first element matching the selector."""
        if not self.page:
            raise PlaywrightError("Browser not started.")
        if not attributes:
             logger.warning("extract_attributes called with empty attributes list.")
             return {"error": "No attributes specified for extraction."} # Return error

        result_dict = {}
        try:
            logger.info(f"Extracting attributes {attributes} from selector: {selector}")
            locator = self.page.locator(selector).first # Get locator

            # Wait briefly for the element to be attached (don't need visibility necessarily for attributes)
            try:
                locator.wait_for(state='attached', timeout=self.default_action_timeout * 0.5)
            except PlaywrightTimeoutError:
                 error_msg = f"Timeout waiting for element '{selector}' to be attached for attribute extraction."
                 logger.error(error_msg)
                 return {"error": error_msg}

            # Element is attached, proceed
            for attr_name in attributes:
                try:
                     # get_attribute doesn't wait, element must exist
                     attr_value = locator.get_attribute(attr_name)
                     result_dict[attr_name] = attr_value
                     logger.debug(f"Extracted attribute '{attr_name}': '{str(attr_value)[:100]}...' from '{selector}'")
                except Exception as attr_e:
                     logger.warning(f"Could not extract attribute '{attr_name}' from '{selector}': {attr_e}")
                     result_dict[attr_name] = f"Error extracting: {attr_e}"

            logger.info(f"Finished extracting attributes {list(result_dict.keys())} from '{selector}'.")
            return result_dict

        except PlaywrightError as e: # Catch errors from wait_for or get_attribute
            logger.error(f"PlaywrightError extracting attributes {attributes} from '{selector}': {e}")
            return {"error": f"PlaywrightError extracting attributes from {selector}: {e}"}
        except Exception as e:
            logger.error(f"Unexpected error extracting attributes {attributes} from '{selector}': {e}", exc_info=True)
            return {"error": f"General error extracting attributes from {selector}: {e}"}


    def save_json_data(self, data: Any, file_path: str) -> dict:
        """
        Saves structured data as a JSON file to the given location.

        Args:
            data: The data to save (typically a dict or list that is JSON serializable).
            file_path: The path/filename where to save the JSON file (e.g., 'output/results.json').

        Returns:
            dict: Status of the operation with success flag, message, and file path.
        """
        try:
            # Ensure the directory exists
            abs_file_path = os.path.abspath(file_path)
            os.makedirs(os.path.dirname(abs_file_path), exist_ok=True)

            # Save the JSON data with pretty formatting
            with open(abs_file_path, 'a', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            logger.info(f"Successfully saved JSON data to {abs_file_path}")
            return {
                "success": True,
                "message": f"Data successfully saved to {abs_file_path}",
                "file_path": abs_file_path
            }
        except TypeError as e:
             logger.error(f"Data provided is not JSON serializable for file {file_path}: {e}", exc_info=True)
             return {
                 "success": False,
                 "message": f"Error saving JSON: Provided data is not serializable ({e})",
                 "error": str(e)
             }
        except Exception as e:
            logger.error(f"Error saving JSON data to {file_path}: {e}", exc_info=True)
            return {
                "success": False,
                "message": f"Error saving JSON data: {e}",
                "error": str(e)
            }

    def _human_like_delay(self, min_secs: float, max_secs: float):
        """ Sleeps for a random duration within the specified range. """
        delay = random.uniform(min_secs, max_secs)
        logger.debug(f"Applying human-like delay: {delay:.2f} seconds")
        time.sleep(delay)
        
    def _get_locator(self, selector: str):
        """
        Gets a Playwright locator for the first matching element,
        handling potential XPath selectors passed as CSS.
        """
        if not self.page:
            raise PlaywrightError("Page is not initialized.")
        if not selector:
            raise ValueError("Selector cannot be empty.")

        # Basic check to see if it looks like XPath
        # Playwright's locator handles 'xpath=...' automatically,
        # but sometimes plain XPaths are passed. Let's try to detect them.
        is_likely_xpath = selector.startswith(('/', '(', '.')) or \
                          ('/' in selector and not any(c in selector for c in ['#', '.', '[', '>', '+', '~', '='])) # Avoid CSS chars

        processed_selector = selector
        if is_likely_xpath and not selector.startswith(('css=', 'xpath=')):
            # If it looks like XPath, explicitly prefix it for Playwright's locator
            logger.debug(f"Selector '{selector}' looks like XPath. Using explicit 'xpath=' prefix.")
            processed_selector = f"xpath={selector}"
        # If it starts with css= or xpath=, Playwright handles it.
        # Otherwise, it's assumed to be a CSS selector.

        try:
            logger.debug(f"Attempting to create locator using: '{processed_selector}'")
            # Use .first to always target a single element, consistent with other actions
            locator = self.page.locator(processed_selector).first
            return locator
        except Exception as e:
            # Catch errors during locator creation itself (e.g., invalid selector syntax)
            logger.error(f"Failed to create locator for processed selector: '{processed_selector}'. Original: '{selector}'. Error: {e}")
            # Re-raise using the processed selector in the message for clarity
            raise PlaywrightError(f"Invalid selector syntax or error creating locator: '{processed_selector}'. Error: {e}") from e