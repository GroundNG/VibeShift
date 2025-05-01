# /src/browser/panel/panel.py
import threading
import logging
from typing import Optional, Dict, Any, List
from playwright.sync_api import sync_playwright, Page, Browser, Playwright, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

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


class Panel:
    """Deals with panel injected into browser in manual mode"""
    def __init__(self, headless=True, page=None):
        self._recorder_ui_injected = False # Track if UI script is injected
        self._panel_interaction_lock = threading.Lock() # Prevent race conditions waiting for panel
        self.headless = headless
        self.page = page
        
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


    