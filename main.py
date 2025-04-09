# main.py
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '.')))
import time

from agent import WebAgent
from llm_client import GeminiClient
from utils import load_api_key
import logging
import warnings

if __name__ == "__main__":
    # logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    logger = logging.getLogger(__name__)

    # --- Security Warning ---
    warnings.warn(
        "SECURITY WARNING: You are about to run an AI agent that interacts with the web based on "
        "LLM instructions. Ensure the tasks given are safe and do not involve sensitive data, logins, "
        "or unintended actions on websites. The agent executes actions suggested by the LLM, which may "
        "not always be predictable or secure. Use with extreme caution and monitor its behavior.",
        UserWarning
    )
    print("\n" + "*"*70)
    print("!!! SECURITY WARNING !!!")
    print("This AI agent interacts with live websites based on LLM suggestions.")
    print("Ensure your tasks are safe and do not involve sensitive information.")
    print("Monitor the agent's actions, especially when running non-headlessly.")
    print("Proceed with caution.")
    print("*"*70 + "\n")
    # --- End Security Warning ---


    try:
        # --- Configuration ---
        api_key = load_api_key()
        # Set headless=False to watch the browser (HIGHLY RECOMMENDED for monitoring)
        HEADLESS_BROWSER = False # Default to False for safety/monitoring
        MAX_AGENT_ITERATIONS = 30 # Allow more steps for longer tasks
        MAX_HISTORY = 8 # Keep history reasonable
        if not os.path.exists("output"):
            try:
                os.makedirs("output")
                logger.info("Created 'output' directory for potential saved files.")
            except OSError as e:
                logger.warning(f"Could not create 'output' directory: {e}. Agent might fail if saving to it.")
        run_headless = input(f"Run in headless mode? (No browser window visible) (yes/NO): ").strip().lower()
        if run_headless != 'yes':
             HEADLESS_BROWSER = False
             print("Running with browser window visible.")
        else:
             HEADLESS_BROWSER = True
             print("Running headless (no browser window).")


        # --- Initialize Components ---
        gemini_client = GeminiClient(api_key=api_key)
        agent = WebAgent(
            gemini_client=gemini_client,
            headless=HEADLESS_BROWSER,
            max_iterations=MAX_AGENT_ITERATIONS,
            max_history_length=MAX_HISTORY
        )

        # --- Get User Goal ---
        print("\nExamples of tasks:")
        print("- Go to Hacker News, login with credentials: OmieChan & Somethingjustlikethis and then find titles and links for the first 5 posts and save them to gemini.json")
        print("- Go to google.com, search for 'best python libraries for web scraping', and list the first 3 results from the organic search listings.")
        print("- Navigate to the Playwright Python documentation website, find the section on 'Page Object Models', and extract the first paragraph.")
        user_goal = input("\nPlease enter the web task for the agent: ")


        # --- Run the Agent ---
        if user_goal:
            agent.run(user_goal)
        else:
             print("No task entered. Exiting.")

    except ValueError as e:
         logger.error(f"Configuration error: {e}")
         print(f"Error: {e}")
    except ImportError as e:
         logger.error(f"Import error: {e}. Make sure you have installed requirements.txt and the script is run correctly.")
         print(f"Import Error: {e}. Please check installation and paths.")
    except Exception as e:
        logger.critical(f"An unexpected error occurred in main: {e}", exc_info=True)
        print(f"An unexpected error occurred: {e}")