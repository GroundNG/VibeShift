# main.py
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '.')))
import time
import json 
import argparse

from agent import WebAgent
from llm_client import GeminiClient
from executor import TestExecutor
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

    # --- Argument Parser ---
    parser = argparse.ArgumentParser(description="AI Web Testing Agent - Recorder & Executor")
    parser.add_argument(
        '--mode',
        choices=['record', 'execute'],
        required=True,
        help="Mode to run the agent in: 'record' (interactive AI-assisted recording) or 'execute' (deterministic playback)."
    )
    parser.add_argument(
        '--file',
        type=str,
        help="Path to the JSON test file (required for 'execute' mode)."
    )
    parser.add_argument(
        '--headless-execution',
        action='store_true', # Makes it a flag, default False
        help="Run executor in headless mode (only applies to 'execute' mode)."
    )
    args = parser.parse_args()

    # Validate arguments based on mode
    if args.mode == 'execute' and not args.file:
        parser.error("--file is required when --mode is 'execute'")
    if args.mode == 'record' and args.file:
        logger.warning("--file argument is ignored in 'record' mode.")
    # --- End Argument Parser ---


    # --- Security Warning ---
    if args.mode == 'record': # Show warning mainly for recording
        warnings.warn(
            "SECURITY WARNING: You are about to run an AI agent that interacts with the web based on "
            "LLM instructions for recording test steps. Ensure the target environment is safe.",
            UserWarning
        )
        print("\n" + "*"*70)
        print("!!! AI WEB TESTING AGENT - RECORDER MODE !!!")
        print("This agent interacts with websites to record automated tests.")
        print(">> Ensure you target the correct environment (e.g., staging).")
        print(">> Avoid recording actions involving highly sensitive production data.")
        print(">> You will be prompted to confirm or override AI suggestions.")
        print("Proceed with caution.")
        print("*"*70 + "\n")
    # --- End Security Warning ---


    try:
        # --- Configuration ---
        api_key = load_api_key()
        if not os.path.exists("output"):
            try:
                os.makedirs("output")
                logger.info("Created 'output' directory for screenshots and evidence.")
            except OSError as e:
                logger.warning(f"Could not create 'output' directory: {e}. Saving evidence/screenshots might fail.")

                
        if args.mode == 'record':
            logger.info("Starting in RECORD mode...")
            HEADLESS_BROWSER = False # Recording MUST be non-headless
            MAX_TEST_ITERATIONS = 50 # Allow more steps for recording complex flows
            MAX_HISTORY_FOR_LLM = 10
            MAX_STEP_RETRIES = 1 # Retries during recording are for AI suggestion refinement

            print("Running in interactive RECORD mode (Browser window is required).")




            # --- Initialize Components ---
            gemini_client = GeminiClient(api_key=api_key)
            recorder_agent = WebAgent(
                gemini_client=gemini_client,
                headless=HEADLESS_BROWSER, # Must be False
                max_iterations=MAX_TEST_ITERATIONS,
                max_history_length=MAX_HISTORY_FOR_LLM,
                max_retries_per_subtask=MAX_STEP_RETRIES,
                is_recorder_mode=True, # Add a flag to agent
                # automated_mode=True
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
                # The run method now handles the recording loop
                recording_result = recorder_agent.record(feature_description) # Changed method name

                print("\n" + "="*20 + " Recording Result " + "="*20)
                if recording_result.get("success"):
                    print(f"Status: SUCCESS")
                    print(f"Recording saved to: {recording_result.get('output_file')}")
                    print(f"Total steps recorded: {recording_result.get('steps_recorded')}")
                else:
                    print(f"Status: FAILED or ABORTED")
                    print(f"Message: {recording_result.get('message')}")
                print("="*58)

            else:
                print("No test case description entered. Exiting.")

        elif args.mode == 'execute':
            logger.info(f"Starting in EXECUTE mode for file: {args.file}")
            HEADLESS_BROWSER = args.headless_execution # Use flag for executor headless
            print(f"Running in EXECUTE mode ({'Headless' if HEADLESS_BROWSER else 'Visible Browser'}).")

            # Executor doesn't need LLM client directly
            executor = TestExecutor(headless=HEADLESS_BROWSER)
            test_result = executor.run_test(args.file)

            # --- Display Test Execution Results ---
            print("\n" + "="*20 + " Execution Result " + "="*20)
            print(f"Test File: {test_result.get('test_file', 'N/A')}")
            print(f"Status: {test_result.get('status', 'UNKNOWN')}")
            print(f"Duration: {test_result.get('duration_seconds', 'N/A')} seconds")
            print(f"Message: {test_result.get('message', 'N/A')}")

            if test_result.get('status') == 'FAIL':
                 print("-" * 15 + " Failure Details " + "-" * 15)
                 failed_step_info = test_result.get('failed_step', {})
                 print(f"Failed Step ID: {failed_step_info.get('step_id', 'N/A')}")
                 print(f"Failed Step Description: {failed_step_info.get('description', 'N/A')}")
                 print(f"Action: {failed_step_info.get('action', 'N/A')}")
                 print(f"Selector Used: {failed_step_info.get('selector', 'N/A')}")
                 print(f"Error: {test_result.get('error_details', 'N/A')}")
                 if test_result.get('screenshot_on_failure'):
                      print(f"Failure Screenshot: {test_result.get('screenshot_on_failure')}")

                 # --- Display Console Errors/Warnings ---
                 console_msgs = test_result.get("console_messages_on_failure", [])
                 if console_msgs:
                     print("\n--- Console Errors/Warnings (Recent): ---")
                     for msg in console_msgs:
                         msg_text = str(msg.get('text',''))
                         print(f"- [{msg.get('type','UNKNOWN').upper()}] {msg_text[:250]}{'...' if len(msg_text) > 250 else ''}")
                     total_err_warn = len([m for m in test_result.get("all_console_messages", []) if m.get('type') in ['error', 'warning']])
                     if total_err_warn > len(console_msgs):
                          print(f"... (Showing last {len(console_msgs)} of {total_err_warn} total errors/warnings. See JSON report for full logs)")
                 else:
                     print("\n--- No relevant console errors/warnings captured on failure. ---")
            elif test_result.get('status') == 'PASS':
                 print(f"Steps Executed: {test_result.get('steps_executed', 'N/A')}")


            print("="*58)

            # --- Save Full Execution Results to JSON ---
            try:
                 base_name = os.path.splitext(os.path.basename(args.file))[0]
                 result_filename = os.path.join("output", f"execution_result_{base_name}_{time.strftime('%Y%m%d_%H%M%S')}.json")
                 with open(result_filename, 'w', encoding='utf-8') as f:
                     json.dump(test_result, f, indent=2, ensure_ascii=False)
                 print(f"\nFull execution result details saved to: {result_filename}")
            except Exception as save_err:
                 logger.error(f"Failed to save full execution result JSON: {save_err}")










    except ValueError as e:
         logger.error(f"Configuration or Input error: {e}")
         print(f"Error: {e}")
    except ImportError as e:
         logger.error(f"Import error: {e}. Make sure requirements are installed and paths correct.")
         print(f"Import Error: {e}. Please check installation.")
    except Exception as e:
        logger.critical(f"An unexpected error occurred in main: {e}", exc_info=True)
        print(f"An critical unexpected error occurred: {e}")
