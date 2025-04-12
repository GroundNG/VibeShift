# main.py
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '.')))
import time
import json # For pretty printing results

from agent import WebAgent
from llm_client import GeminiClient
from utils import load_api_key
import logging
import warnings

if __name__ == "__main__":
    # Configure logging (DEBUG for detailed logs, INFO for less)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    # logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    # Suppress noisy logs from specific libraries if needed
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.INFO) # Show Playwright info but not debug

    logger = logging.getLogger(__name__) # Logger for main script

    # --- Security Warning (Still relevant!) ---
    warnings.warn(
        "SECURITY WARNING: You are about to run an AI agent that interacts with the web based on "
        "LLM instructions for testing purposes. Ensure the test cases given are safe, target the intended "
        "environment (e.g., staging vs. production), and do not involve unintended data exposure or modifications.",
        UserWarning
    )
    print("\n" + "*"*70)
    print("!!! AI WEB TESTING AGENT - SECURITY WARNING !!!")
    print("This agent interacts with websites to perform automated tests.")
    print(">> Ensure test cases target the correct environment (e.g., staging).")
    print(">> Avoid tests involving highly sensitive production data or actions.")
    print(">> Monitor the agent's actions, especially when running non-headlessly.")
    print("Proceed with caution.")
    print("*"*70 + "\n")

    # --- End Security Warning ---


    try:
        # --- Configuration ---
        api_key = load_api_key()
        HEADLESS_BROWSER = False # Default to False for monitoring tests
        MAX_TEST_ITERATIONS = 30 # Max steps allowed per test case
        MAX_HISTORY_FOR_LLM = 8
        MAX_STEP_RETRIES = 1 # How many times to retry a single failed step (0 or 1 typical)

        if not os.path.exists("output"):
            try:
                os.makedirs("output")
                logger.info("Created 'output' directory for screenshots and evidence.")
            except OSError as e:
                logger.warning(f"Could not create 'output' directory: {e}. Saving evidence/screenshots might fail.")

        # Ask user about headless mode
        run_headless_input = input(f"Run in headless mode? (No browser window visible - faster but harder to watch) (yes/NO): ").strip().lower()
        if run_headless_input == 'yes':
             HEADLESS_BROWSER = True
             print("Running headless.")
        else:
             HEADLESS_BROWSER = False
             print("Running with browser window visible (recommended for monitoring).")


        # --- Initialize Components ---
        gemini_client = GeminiClient(api_key=api_key)
        agent = WebAgent(
            gemini_client=gemini_client,
            headless=HEADLESS_BROWSER,
            max_iterations=MAX_TEST_ITERATIONS,
            max_history_length=MAX_HISTORY_FOR_LLM,
            max_retries_per_subtask=MAX_STEP_RETRIES # Pass retry setting
        )

        # --- Get Feature Description ---
        print("\nEnter the feature or user flow you want to test.")
        print("Examples:")
        print("- go to https://practicetestautomation.com/practice-test-login/ and login with username as student and password as Password123 and verify if the login was successful")
        print("- Navigate to 'https://example-shop.com', search for 'blue widget', add the first result to the cart, and verify the cart item count increases to 1 (selector: 'span#cart-count').")
        print("- On 'https://form-page.com', fill the 'email' field with 'test@example.com', check the 'terms' checkbox (id='terms-cb'), click submit, and verify the success message 'Form submitted!' is shown in 'div.status'.")

        feature_description = input("\nPlease enter the test case description: ")

        # --- Run the Test ---
        if feature_description:
            test_result = agent.run(feature_description)

            # --- Display Test Results ---
            print("\n" + "="*20 + " Test Result " + "="*20)
            print(f"Feature Tested: {test_result.get('feature', 'N/A')}")
            print(f"Status: {test_result.get('status', 'UNKNOWN')}")
            print(f"Duration: {test_result.get('duration_seconds', 'N/A')} seconds")
            print(f"Message: {test_result.get('message', 'N/A')}")

            if test_result.get('status') == 'FAIL':
                 print("-" * 15 + " Failure Details " + "-" * 15)
                 failed_idx = test_result.get('failed_step_index')
                 print(f"Failed Step #: {failed_idx + 1 if failed_idx is not None else 'N/A'}")
                 print(f"Failed Step Description: {test_result.get('failed_step_description', 'N/A')}")
                 print(f"Error: {test_result.get('error_details', 'N/A')}")
                 if test_result.get('screenshot_on_failure'):
                      print(f"Failure Screenshot: {test_result.get('screenshot_on_failure')}")

                 # --- Display Console Errors/Warnings ---
                 console_msgs = test_result.get("console_messages_on_failure", [])
                 if console_msgs:
                     print("\n--- Console Errors/Warnings (Recent): ---")
                     for msg in console_msgs:
                         # Basic formatting, ensure text is string
                         msg_text = str(msg.get('text',''))
                         print(f"- [{msg.get('type','UNKNOWN').upper()}] {msg_text[:250]}{'...' if len(msg_text) > 250 else ''}") # Truncate long messages
                     # Indicate if more messages are in the full log
                     total_err_warn = len([m for m in test_result.get("all_console_messages", []) if m.get('type') in ['error', 'warning']])
                     if total_err_warn > len(console_msgs):
                          print(f"... (Showing last {len(console_msgs)} of {total_err_warn} total errors/warnings. See JSON report for full logs)")
                 else:
                     print("\n--- No relevant console errors/warnings captured on failure. ---")
                 # -------------------------------------

            print("-" * 15 + " Steps Summary " + "-" * 15)
            print(test_result.get('steps_summary', 'N/A'))

            if test_result.get('output_file'):
                 print(f"Evidence File: {test_result.get('output_file')}")

            print("="*53)

            # --- Save Full Results to JSON ---
            try:
                result_filename = os.path.join("output", f"test_result_{time.strftime('%Y%m%d_%H%M%S')}.json")
                # Ensure messages are serializable (they should be dicts)
                with open(result_filename, 'w', encoding='utf-8') as f:
                    json.dump(test_result, f, indent=2, ensure_ascii=False)
                print(f"\nFull test result details (including all console messages) saved to: {result_filename}")
            except Exception as save_err:
                logger.error(f"Failed to save full test result JSON: {save_err}")

        else:
             print("No test case description entered. Exiting.")

    except ValueError as e:
         logger.error(f"Configuration or Input error: {e}")
         print(f"Error: {e}")
    except ImportError as e:
         logger.error(f"Import error: {e}. Make sure requirements are installed and paths correct.")
         print(f"Import Error: {e}. Please check installation.")
    except Exception as e:
        logger.critical(f"An unexpected error occurred in main: {e}", exc_info=True)
        print(f"An critical unexpected error occurred: {e}")
