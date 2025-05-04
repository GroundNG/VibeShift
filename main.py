# main.py
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '.')))
import time
import json 
import argparse

from src.agents.recorder_agent import WebAgent
from src.agents.crawler_agent import CrawlerAgent
from src.llm.llm_client import LLMClient
from src.execution.executor import TestExecutor
from src.utils.utils import load_api_key, load_api_version, load_api_base_url, load_llm_model
from src.agents.auth_agent import record_selectors_and_save_auth_state
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
        choices=['record', 'execute','auth' ,'discover'],
        required=True,
        help="Mode to run the agent in: 'record' (interactive AI-assisted recording) or 'execute' (deterministic playback)."
    )
    parser.add_argument(
        '--file',
        type=str,
        help="Path to the JSON test file (required for 'execute' mode)."
    )
    parser.add_argument(
        '--headless',
        action='store_true', # Makes it a flag, default False
        help="Run executor in headless mode (only applies to 'execute'/discover mode)."
    )
    parser.add_argument(
        '--url', # <<< Added URL argument for discover mode
        type=str,
        help="Starting URL for website crawling (required for 'discover' mode)."
    )
    parser.add_argument(
        '--max-pages', # <<< Added max pages argument for discover mode
        type=int,
        default=10,
        help="Maximum number of pages to crawl in 'discover' mode (default: 10)."
    )
    parser.add_argument(
        '--automated',
        action='store_true', # Use action='store_true' for boolean flags
        help="Run recorder in automated mode (AI makes decisions without user prompts). Only applies to 'record' mode." # Clarified help text
    )
    parser.add_argument(
        '--enable-healing',
        action='store_true',
        help="Enable self-healing during execution ('execute' mode only)."
    )
    parser.add_argument(
        '--healing-mode',
        choices=['soft', 'hard'],
        default='soft',
        help="Self-healing mode: 'soft' (fix selector) or 'hard' (re-record) ('execute' mode only)."
    )
    parser.add_argument('--provider', choices=['gemini', 'openai', 'azure'], default='gemini', help="LLM provider (default: gemini). Choose openai for any OpenAI compatible LLMs.")
    args = parser.parse_args()

    # Validate arguments based on mode
    if args.mode == 'execute':
        if not args.file:
            parser.error("--file is required when --mode is 'execute'")
        if not args.enable_healing and args.healing_mode != 'soft':
             logger.warning("--healing-mode is ignored when --enable-healing is not set.")
    elif args.mode == 'record':
        if args.enable_healing:
             logger.warning("--enable-healing and --healing-mode are ignored in 'record' mode.")
    elif args.mode == 'discover':
        if not args.url:
             parser.error("--url is required when --mode is 'discover'")
        if args.enable_healing:
            logger.warning("--enable-healing and --healing-mode are ignored in 'discover' mode.")
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
        endpoint = load_api_base_url()
        api_version = load_api_version()
        model_name = load_llm_model()
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
            llm_client = LLMClient(provider=args.provider)
            
            automated = False
            if args.automated == True:
                automated = True
            recorder_agent = WebAgent(
                llm_client=llm_client,
                headless=HEADLESS_BROWSER, # Must be False
                max_iterations=MAX_TEST_ITERATIONS,
                max_history_length=MAX_HISTORY_FOR_LLM,
                max_retries_per_subtask=MAX_STEP_RETRIES,
                is_recorder_mode=True, # Add a flag to agent
                automated_mode=automated
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
            HEADLESS_BROWSER = args.headless # Use flag for executor headless
            PIXEL_MISMATCH_THRESHOLD = 0.01
            heal_msg = f"Self-Healing: {'ENABLED (' + args.healing_mode + ' mode)' if args.enable_healing else 'DISABLED'}"
            print(f"Running in EXECUTE mode ({'Headless' if args.headless else 'Visible Browser'}). {heal_msg}")

            
            llm_client = LLMClient(provider=args.provider)

            # Executor doesn't need LLM client directly
            executor = TestExecutor(
                llm_client=llm_client, # Pass the initialized client
                headless=args.headless,
                enable_healing=args.enable_healing,
                healing_mode=args.healing_mode,
                pixel_threshold=PIXEL_MISMATCH_THRESHOLD,
                get_performance=True,
                get_network_requests=True   
                # healing_retries can be added as arg if needed
            )
            test_result = executor.run_test(args.file)

            # --- Display Test Execution Results ---
            print("\n" + "="*20 + " Execution Result " + "="*20)
            print(f"Test File: {test_result.get('test_file', 'N/A')}")
            print(f"Status: {test_result.get('status', 'UNKNOWN')}")
            print(f"Duration: {test_result.get('duration_seconds', 'N/A')} seconds")
            print(f"Message: {test_result.get('message', 'N/A')}")
            print(f"Healing: {'ENABLED ('+test_result.get('healing_mode','N/A')+' mode)' if test_result.get('healing_enabled') else 'DISABLED'}")
            
            perf_timing = test_result.get("performance_timing")
            if perf_timing:
                 try:
                      nav_start = perf_timing.get('navigationStart', 0)
                      load_end = perf_timing.get('loadEventEnd', 0)
                      dom_content_loaded = perf_timing.get('domContentLoadedEventEnd', 0)
                      dom_interactive = perf_timing.get('domInteractive', 0)

                      if nav_start > 0: # Ensure navigationStart is valid
                           print("\n--- Performance Metrics (Initial Load) ---")
                           if load_end > nav_start: print(f"  Page Load Time (loadEventEnd): {(load_end - nav_start):,}ms")
                           if dom_content_loaded > nav_start: print(f"  DOM Content Loaded (domContentLoadedEventEnd): {(dom_content_loaded - nav_start):,}ms")
                           if dom_interactive > nav_start: print(f"  DOM Interactive: {(dom_interactive - nav_start):,}ms")
                           print("-" * 20)
                      else:
                           print("\n--- Performance Metrics (Initial Load): navigationStart not captured ---")
                 except Exception as perf_err:
                     logger.warning(f"Could not process performance timing: {perf_err}")
                     print("\n--- Performance Metrics: Error processing data ---")
            # ------------------------------------

            # --- Network Request Summary ---
            network_reqs = test_result.get("network_requests", [])
            if network_reqs:
                 print("\n--- Network Summary ---")
                 total_reqs = len(network_reqs)
                 http_error_reqs = len([r for r in network_reqs if (r.get('status', 0) or 0) >= 400])
                 error_reqs = len([r for r in network_reqs if (r.get('status', 0) or 0) >= 400])
                 slow_reqs = len([r for r in network_reqs if (r.get('duration_ms') or 0) > 1500]) # Example: > 1.5s

                 print(f"  Total Requests: {total_reqs}")
                 if http_error_reqs > 0: print(f"  Requests >= 400 Status: {http_error_reqs}")
                 if error_reqs > 0: print(f"  Requests >= 400 Status: {error_reqs}")
                 if slow_reqs > 0: print(f"  Requests > 1500ms: {slow_reqs}")
                 print("(See JSON report for full network details)")
                 print("-" * 20)
            
            visual_results = test_result.get("visual_assertion_results", [])
            if visual_results:
                 print("\n--- Visual Assertion Results ---")
                 for vr in visual_results:
                     status = vr.get('status', 'UNKNOWN')
                     override = " (LLM Override)" if vr.get('llm_override') else ""
                     diff_percent = vr.get('pixel_difference_ratio', 0) * 100
                     thresh_percent = vr.get('pixel_threshold', PIXEL_MISMATCH_THRESHOLD) * 100 # Use executor's default if needed
                     print(f"- Step {vr.get('step_id')}, Baseline '{vr.get('baseline_id')}': {status}{override}")
                     print(f"  Pixel Difference: {diff_percent:.4f}% (Threshold: {thresh_percent:.2f}%)")
                     if status == 'FAIL':
                         if vr.get('diff_image_path'):
                             print(f"  Diff Image: {vr.get('diff_image_path')}")
                         if vr.get('llm_reasoning'):
                             print(f"  LLM Reasoning: {vr.get('llm_reasoning')}")
                     elif vr.get('llm_override'): # Passed due to LLM
                           if vr.get('llm_reasoning'):
                             print(f"  LLM Reasoning: {vr.get('llm_reasoning')}")

                 print("-" * 20)

            # Display Healing Attempts Log
            healing_attempts = test_result.get("healing_attempts", [])
            if healing_attempts:
                 print("\n--- Healing Attempts ---")
                 for attempt in healing_attempts:
                     outcome = "SUCCESS" if attempt.get('success') else "FAIL"
                     mode = attempt.get('mode', 'N/A')
                     print(f"- Step {attempt.get('step_id')}: Attempt {attempt.get('attempt')} ({mode} mode) - {outcome}")
                     if outcome == "SUCCESS" and mode == "soft":
                          print(f"  Old Selector: {attempt.get('failed_selector')}")
                          print(f"  New Selector: {attempt.get('new_selector')}")
                          print(f"  Reasoning: {attempt.get('reasoning', 'N/A')[:100]}...")
                     elif outcome == "FAIL" and mode == "soft":
                          print(f"  Failed Selector: {attempt.get('failed_selector')}")
                          print(f"  Reasoning: {attempt.get('reasoning', 'N/A')[:100]}...")
                     elif mode == "hard":
                          print(f"  Triggered re-recording due to error: {attempt.get('error', 'N/A')[:100]}...")
                 print("-" * 20)

            if test_result.get('status') == 'FAIL':
                 print("-" * 15 + " Failure Details " + "-" * 15)
                 failed_step_info = test_result.get('failed_step', {})
                 print(f"Failed Step ID: {failed_step_info.get('step_id', 'N/A')}")
                 print(f"Failed Step Description: {failed_step_info.get('description', 'N/A')}")
                 print(f"Action: {failed_step_info.get('action', 'N/A')}")
                 # Show the *last* selector tried if healing was attempted
                 last_selector_tried = failed_step_info.get('selector') # Default to original
                 last_failed_healing_attempt = next((a for a in reversed(healing_attempts) if a.get('step_id') == failed_step_info.get('step_id') and not a.get('success')), None)
                 if last_failed_healing_attempt:
                      last_selector_tried = last_failed_healing_attempt.get('failed_selector')
                 print(f"Selector Used (Last Attempt): {last_selector_tried or 'N/A'}")
                 print(f"Error: {test_result.get('error_details', 'N/A')}")
                 if test_result.get('screenshot_on_failure'):
                      print(f"Failure Screenshot: {test_result.get('screenshot_on_failure')}")
                 # (Console message display remains the same)
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
            elif test_result.get('status') == 'HEALING_TRIGGERED':
                 print(f"\nNOTICE: Hard Healing (re-recording) was triggered.")
                 print(f"The original execution stopped at Step {test_result.get('failed_step', {}).get('step_id', 'N/A')}.")
                 print(f"Check logs for the status and output file of the re-recording process.")


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

        elif args.mode == 'discover':
            warnings.warn(
                "SECURITY WARNING: You are about to run an AI agent that interacts with the web based on "
                "LLM instructions or crawling logic. Ensure the target environment is safe.",
                UserWarning
            )   
            print("!!! AI WEB TESTING AGENT - DISCOVERY MODE !!!")
            print("This agent will crawl the website starting from the provided URL.")
            print(">> It will analyze pages and ask an LLM for test step ideas.")
            print(">> Ensure you have permission to crawl the target website.")
            print(f">> Crawling will be limited to the domain of '{args.url}' and max {args.max_pages} pages.")
            print("Proceed with caution.")
            print("*"*70 + "\n")
            logger.info(f"Starting in DISCOVER mode for URL: {args.url}")
            HEADLESS_BROWSER = args.headless # Use the general headless flag
            print(f"Running in DISCOVER mode ({'Headless' if HEADLESS_BROWSER else 'Visible Browser'}).")
            print(f"Starting URL: {args.url}")
            print(f"Max pages to crawl: {args.max_pages}")

            # Initialize Components
            llm_client = LLMClient(provider=args.provider)
            crawler = CrawlerAgent(
                llm_client=llm_client,
                headless=HEADLESS_BROWSER
            )

            # Run Discovery
            discovery_result = crawler.crawl_and_suggest(args.url, args.max_pages)

            # Display Discovery Results
            print("\n" + "="*20 + " Discovery Result " + "="*20)
            print(f"Status: {'SUCCESS' if discovery_result.get('success') else 'FAILED'}")
            print(f"Message: {discovery_result.get('message', 'N/A')}")
            print(f"Start URL: {discovery_result.get('start_url', 'N/A')}")
            print(f"Base Domain: {discovery_result.get('base_domain', 'N/A')}")
            print(f"Pages Visited: {discovery_result.get('pages_visited', 0)}")

            discovered_steps_map = discovery_result.get('discovered_steps', {})
            print(f"Pages with Suggested Steps: {len(discovered_steps_map)}")
            print("-" * 58)

            if discovered_steps_map:
                print("\n--- Suggested Test Steps per Page ---")
                for page_url, steps in discovered_steps_map.items():
                    print(f"\n[Page: {page_url}]")
                    if steps:
                        for i, step_desc in enumerate(steps):
                            print(f"  {i+1}. {step_desc}")
                    else:
                        print("  (No specific steps suggested by LLM for this page)")
            else:
                print("\nNo test step suggestions were generated.")

            print("="*58)

            # Save Full Discovery Results to JSON
            if discovery_result.get('success'): # Only save if crawl succeeded somewhat
                try:
                     # Generate a filename based on the domain
                     domain = discovery_result.get('base_domain', 'unknown_domain')
                     # Sanitize domain for filename
                     safe_domain = "".join(c if c.isalnum() else "_" for c in domain)
                     result_filename = os.path.join("output", f"discovery_results_{safe_domain}_{time.strftime('%Y%m%d_%H%M%S')}.json")
                     with open(result_filename, 'w', encoding='utf-8') as f:
                         json.dump(discovery_result, f, indent=2, ensure_ascii=False)
                     print(f"\nFull discovery result details saved to: {result_filename}")
                except Exception as save_err:
                     logger.error(f"Failed to save full discovery result JSON: {save_err}")

        elif args.mode == 'auth':
            # Ensure output directory exists
            os.makedirs("output", exist_ok=True)

            # --- IMPORTANT: Initialize your LLM Client here ---
            # Replace with your actual LLM provider and initialization
            try:
                # Example using Gemini (replace with your actual setup)
                # Ensure GOOGLE_API_KEY is set as an environment variable if using GeminiClient defaults
                logger.info(f"Using LLM Provider: {args.provider}")
                llm = LLMClient(provider=args.provider)
                logger.info("LLM Client initialized.")
            except ValueError as e:
                logger.error(f"❌ Failed to initialize LLM Client: {e}. Cannot proceed.")
                llm = None
            except Exception as e:
                logger.error(f"❌ An unexpected error occurred initializing LLM Client: {e}. Cannot proceed.", exc_info=True)
                llm = None
            # ------------------------------------------------

            if llm:
                success = record_selectors_and_save_auth_state(llm, args.url, args.file)
                if success:
                    print(f"\n--- Authentication state generation completed successfully. ---")
                else:
                    print(f"\n--- Authentication state generation failed. Check logs and screenshots in 'output/'. ---")
            else:
                print("\n--- Could not initialize LLM Client. Aborting authentication state generation. ---")







    except ValueError as e:
         logger.error(f"Configuration or Input error: {e}")
         print(f"Error: {e}")
    except ImportError as e:
         logger.error(f"Import error: {e}. Make sure requirements are installed and paths correct.")
         print(f"Import Error: {e}. Please check installation.")
    except Exception as e:
        logger.critical(f"An unexpected error occurred in main: {e}", exc_info=True)
        print(f"An critical unexpected error occurred: {e}")
