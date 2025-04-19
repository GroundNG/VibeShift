# mcp_server.py
import sys
import os
import json
import logging
from typing import List, Dict, Any, Optional
import asyncio

# Ensure agent modules are importable (adjust path if necessary)
# Assuming mcp_server.py is at the root level alongside agent.py etc.
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.prompts import base as mcp_prompts

# Import necessary components from your existing code
from agent import WebAgent # Needs refactoring for non-interactive use
from llm_client import GeminiClient
from executor import TestExecutor
from utils import load_api_key

# Configure logging for the MCP server
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - [MCP Server] %(message)s')
logger = logging.getLogger(__name__)

# Define the output directory for tests (consistent with agent/executor)
TEST_OUTPUT_DIR = "output"

# --- Initialize FastMCP Server ---
mcp = FastMCP("WebTestAgentServer")

# --- Helper to Load API Key ---
# Cache the key to avoid loading it repeatedly within the server's lifetime
API_KEY = None
def get_api_key() -> str:
    global API_KEY
    if API_KEY is None:
        try:
            API_KEY = load_api_key()
        except ValueError as e:
            logger.error(f"Failed to load API key: {e}")
            raise  # Re-raise to prevent server from running without key
    return API_KEY

# --- MCP Tool: Record a New Test Flow (Automated - Requires Agent Refactoring) ---
@mcp.tool()
async def record_test_flow(feature_description: str, headless: bool = True) -> Dict[str, Any]:
    """
    Attempts to automatically record a web test flow based on a natural language description.
    Uses the WebAgent in automated mode (bypasses interactive prompts).

    Args:
        feature_description: A natural language description of the test case or user flow. Crucially, this description MUST explicitly include the starting URL of the website to be tested (e.g., 'Go to https://example.com, then click...').
        headless: Run the underlying browser in headless mode. Defaults to True.

    Returns:
        A dictionary containing the recording status, including success/failure,
        message, and the path to the generated test JSON file if successful.
    """
    logger.info(f"Received automated request to record test flow: '{feature_description[:100]}...' (Headless: {headless})")
    try:
        # 1. Initialize required components
        api_key = get_api_key()
        gemini_client = GeminiClient(api_key=api_key)

        # 2. Instantiate WebAgent in AUTOMATED mode
        recorder_agent = WebAgent(
            gemini_client=gemini_client,
            headless=headless, # Allow MCP tool to specify headless
            is_recorder_mode=True,
            automated_mode=True, # <<< Set automated mode 
            max_retries_per_subtask=2 
        )
        
        # Run the blocking recorder_agent.record method in a separate thread
        # Pass the method and its arguments to asyncio.to_thread
        logger.info("Delegating agent recording to a separate thread...")
        recording_result = await asyncio.to_thread(recorder_agent.record, feature_description)
        logger.info(f"Automated recording finished (thread returned). Result: {recording_result}")
        return recording_result

    except Exception as e:
        logger.error(f"Error in record_test_flow tool: {e}", exc_info=True)
        return {"success": False, "message": f"Internal server error during automated recording: {e}"}


# --- MCP Tool: Run a Single Regression Test ---
@mcp.tool()
async def run_regression_test(test_file_path: str, headless: bool = True) -> Dict[str, Any]:
    """
    Runs a previously recorded test case from a JSON file.

    Args:
        test_file_path: The relative or absolute path to the .json test file (e.g., 'output/test_login.json').
        headless: Run the browser in headless mode (no visible window). Defaults to True.

    Returns:
        A dictionary containing the execution result summary, including status (PASS/FAIL),
        duration, message, error details (if failed), and evidence paths.
    """
    logger.info(f"Received request to run regression test: '{test_file_path}', Headless: {headless}")

    # Basic path validation (relative to server or absolute)
    if not os.path.isabs(test_file_path):
        # Assume relative to the server's working directory or a known output dir
        # For simplicity, let's check relative to CWD and TEST_OUTPUT_DIR
        potential_paths = [
            test_file_path,
            os.path.join(TEST_OUTPUT_DIR, test_file_path)
        ]
        found_path = None
        for p in potential_paths:
            if os.path.exists(p) and os.path.isfile(p):
                found_path = p
                break
        if not found_path:
             logger.error(f"Test file not found at '{test_file_path}' or within '{TEST_OUTPUT_DIR}'.")
             return {"success": False, "status": "ERROR", "message": f"Test file not found: {test_file_path}"}
        test_file_path = os.path.abspath(found_path) # Use absolute path for executor
        logger.info(f"Resolved test file path to: {test_file_path}")


    try:
        # Executor doesn't need the LLM client
        executor = TestExecutor(headless=headless)
        logger.info(f"Delegating test execution for '{test_file_path}' to a separate thread...")
        test_result = await asyncio.to_thread(
            executor.run_test, # The function to run
            test_file_path     # Arguments for the function
        )

        # Add a success flag for generic tool success/failure indication
        # Post-processing (synchronous)
        test_result["success"] = test_result.get("status") == "PASS"
        logger.info(f"Execution finished for '{test_file_path}' (thread returned). Status: {test_result.get('status')}")
        return test_result

    except FileNotFoundError:
        logger.error(f"Test file not found by executor: {test_file_path}")
        return {"success": False, "status": "ERROR", "message": f"Test file not found: {test_file_path}"}
    except Exception as e:
        logger.error(f"Error running regression test '{test_file_path}': {e}", exc_info=True)
        return {"success": False, "status": "ERROR", "message": f"Internal server error during execution: {e}"}

# --- MCP Resource: List Recorded Tests ---
@mcp.resource("tests:///list")
def list_recorded_tests() -> List[str]:
    """
    Provides a list of available test JSON files in the standard output directory.

    Returns:
        A list of filenames (e.g., ["test_login_flow_....json", "test_search_....json"]).
    """
    logger.info(f"Providing resource list of tests from '{TEST_OUTPUT_DIR}'")
    if not os.path.exists(TEST_OUTPUT_DIR) or not os.path.isdir(TEST_OUTPUT_DIR):
        logger.warning(f"Test output directory '{TEST_OUTPUT_DIR}' not found.")
        return []

    try:
        test_files = [
            f for f in os.listdir(TEST_OUTPUT_DIR)
            if os.path.isfile(os.path.join(TEST_OUTPUT_DIR, f)) and f.endswith(".json") and f.startswith(("test_", "simulated_test_", "execution_result_"))
        ]
        # Optionally return just the test files, excluding execution results
        test_files = [f for f in test_files if not f.startswith("execution_result_")]
        return sorted(test_files)
    except Exception as e:
        logger.error(f"Error listing test files in '{TEST_OUTPUT_DIR}': {e}", exc_info=True)
        # Re-raise or return empty list? Returning empty is safer for resource.
        return []


# --- MCP Prompt: Guide for Requesting a Test Recording ---
@mcp.prompt()
def explain_test_request(feature_description: str) -> list[mcp_prompts.Message]:
    """
    Generates a prompt to guide the calling LLM (e.g., coding assistant)
    on how to use the 'record_test_flow' tool.
    """
    return [
        mcp_prompts.UserMessage(
            "You can ask me to record a web test flow based on a description. "
            "Please provide a clear, step-by-step description of the user journey you want to test. "
            "For example: 'Navigate to https://example.com/login, enter 'user' in the username field (selector: #user), 'pass' in the password field (selector: #pass), click the login button (selector: button.login), and verify the text 'Welcome!' appears (selector: h1.welcome)'."
        ),
        mcp_prompts.UserMessage(
            f"Based on your request: '{feature_description}', I will attempt to use the 'record_test_flow' tool. "
        ),
    ]


# --- Running the Server ---
# The actual running is handled by `mcp dev` or `mcp install`.
# No `if __name__ == "__main__": mcp.run()` needed here when using the CLI tools.
logger.info("WebTestAgent MCP Server defined. Run with 'mcp dev mcp_server.py'")

if __name__ == "__main__":
    mcp.run()