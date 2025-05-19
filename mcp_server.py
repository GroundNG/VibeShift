# mcp_server.py
import sys
import os
import json
import logging
from typing import List, Dict, Any, Optional
import asyncio
import re
import time
from datetime import datetime

# Ensure agent modules are importable (adjust path if necessary)
# Assuming mcp_server.py is at the root level alongside agent.py etc.
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.prompts import base as mcp_prompts

# Import necessary components from your existing code
from src.agents.recorder_agent import WebAgent # Needs refactoring for non-interactive use
from src.agents.crawler_agent import CrawlerAgent
from src.llm.llm_client import LLMClient
from src.execution.executor import TestExecutor
from src.utils.utils import load_api_key, load_api_base_url, load_api_version, load_llm_model
from src.security.semgrep_scanner import run_semgrep
from src.security.zap_scanner import run_zap_scan, discover_endpoints
from src.security.nuclei_scanner import run_nuclei
from src.security.utils import save_report

# Configure logging for the MCP server
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - [MCP Server] %(message)s')
logger = logging.getLogger(__name__)

# Define the output directory for tests (consistent with agent/executor)
TEST_OUTPUT_DIR = "output"

# --- Initialize FastMCP Server ---
mcp = FastMCP("WebTestAgentServer")

llm_client = LLMClient(provider='azure')

# --- MCP Tool: Record a New Test Flow (Automated - Requires Agent Refactoring) ---
@mcp.tool()
async def record_test_flow(feature_description: str, project_directory: str, headless: bool = True) -> Dict[str, Any]:
    """
    Attempts to automatically record a web test flow based on a natural language description. If a case fails, there might be a possibility that you missed/told wrong step in feature description. Don't give vague actions like select anything. Give exact actions like select so and so element
    Uses the WebAgent in automated mode (bypasses interactive prompts). Do not skip telling any step. Give complete end to end steps what to do and what to verify

    Args:
        feature_description: A natural language description of the test case or user flow. Crucially, this description MUST explicitly include the starting URL of the website to be tested (e.g., 'Go to https://example.com, then click...'). Do not give blanket things for input. Say exact things like enter invalid-email into email box or enter validemail@gmail.com into mailbox
        project_directory: The project directory you are currently working in. This is used to identify the test flows of a project
        headless: Run the underlying browser in headless mode. Defaults to True.

    Returns:
        A dictionary containing the recording status, including success/failure,
        message, and the path to the generated test JSON file if successful.
    """
    logger.info(f"Received automated request to record test flow: '{feature_description[:100]}...' (Headless: {headless})")
    try:
        
        # 1. Instantiate WebAgent in AUTOMATED mode
        recorder_agent = WebAgent(
            llm_client=llm_client,
            headless=headless, # Allow MCP tool to specify headless
            is_recorder_mode=True,
            automated_mode=True, # <<< Set automated mode 
            max_retries_per_subtask=2,
            filename=re.sub(r"[ /]", "_", project_directory)
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
async def run_regression_test(test_file_path: str, headless: bool = True, enable_healing: bool = True, healing_mode: str = 'soft', get_performance: bool = False, get_network_requests: bool = False) -> Dict[str, Any]:
    """
    Runs a previously recorded test case from a JSON file. If a case fails, it could be either because your code has a problem, or could be you missed/wrong step in feature description
    

    Args:
        test_file_path: The relative or absolute path to the .json test file (e.g., 'output/test_login.json').
        headless: Run the browser in headless mode (no visible window). Defaults to True.
        enable_healing: Whether to run this regression test with healing mode enabled. In healing mode, if test fails because of a changed or flaky selector, the agent can try to heal the test automatically.
        healing_mode: can be 'soft' or 'hard'. In soft mode, only single step is attempted to heal. In hard healing, complete test is tried to be re-recorded
        get_performance: Whether to include performance stats in response
        get_network_requests: Whether to include network stats in response

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
        executor = TestExecutor(
            headless=headless, 
            llm_client=llm_client, 
            enable_healing=enable_healing,
            healing_mode=healing_mode,
            get_network_requests=get_network_requests,
            get_performance=get_performance
            )
        logger.info(f"Delegating test execution for '{test_file_path}' to a separate thread...")
        test_result = await asyncio.to_thread(
            executor.run_test, # The function to run
            test_file_path     # Arguments for the function
        )

        # Add a success flag for generic tool success/failure indication
        # Post-processing (synchronous)
        test_result["success"] = test_result.get("status") == "PASS"
        logger.info(f"Execution finished for '{test_file_path}' (thread returned). Status: {test_result.get('status')}")
        try:
                base_name = os.path.splitext(os.path.basename(test_file_path))[0]
                result_filename = os.path.join("output", f"execution_result_{base_name}_{time.strftime('%Y%m%d_%H%M%S')}.json")
                with open(result_filename, 'w', encoding='utf-8') as f:
                    json.dump(test_result, f, indent=2, ensure_ascii=False)
                print(f"\nFull execution result details saved to: {result_filename}")
        except Exception as save_err:
                logger.error(f"Failed to save full execution result JSON: {save_err}")
        return test_result

    except FileNotFoundError:
        logger.error(f"Test file not found by executor: {test_file_path}")
        return {"success": False, "status": "ERROR", "message": f"Test file not found: {test_file_path}"}
    except Exception as e:
        logger.error(f"Error running regression test '{test_file_path}': {e}", exc_info=True)
        return {"success": False, "status": "ERROR", "message": f"Internal server error during execution: {e}"}

@mcp.tool()
async def discover_test_flows(start_url: str, max_pages_to_crawl: int = 10, headless: bool = True) -> Dict[str, Any]:
    """
     Crawls a website starting from a given URL within the same domain, analyzes page content
    (DOM, Screenshot), and uses an LLM to suggest potential specific test step descriptions
    for each discovered page.

    Args:
        start_url: The URL to begin crawling from (e.g., 'https://example.com').
        max_pages_to_crawl: The maximum number of unique pages to visit (default: 10).
        headless: Run the crawler's browser in headless mode (default: True).

    Returns:
        A dictionary containing the crawl summary, including success status,
        pages visited, and a dictionary mapping visited URLs to suggested test step descriptions.
        Example: {"success": true, "discovered_steps": {"https://example.com/login": ["Type 'user' into Username field", ...]}}
    """
    logger.info(f"Received request to discover test flows starting from: '{start_url}', Max Pages: {max_pages_to_crawl}, Headless: {headless}")

    try:
        # 1. Instantiate CrawlerAgent
        crawler = CrawlerAgent(
            llm_client=llm_client,
            headless=headless
        )

        # 2. Run the blocking crawl method in a separate thread
        logger.info("Delegating crawler execution to a separate thread...")
        crawl_results = await asyncio.to_thread(
            crawler.crawl_and_suggest,
            start_url,
            max_pages_to_crawl
        )
        logger.info(f"Crawling finished (thread returned). Visited: {crawl_results.get('pages_visited')}, Suggestions: {len(crawl_results.get('discovered_steps', {}))}")


        # Return the results dictionary from the crawler
        return crawl_results

    except Exception as e:
        logger.error(f"Error in discover_test_flows tool: {e}", exc_info=True)
        return {"success": False, "message": f"Internal server error during crawling: {e}", "discovered_steps": {}}


# --- MCP Resource: List Recorded Tests ---
@mcp.tool()
def list_recorded_tests(project_directory: str) -> List[str]:
    """
    Provides a list of available test JSON files in the standard output directory.

    Args:
    project_directory: The project directory you are currently working in. This is used to identify the test flows of a project
    
    Returns:
        test_files: A list of filenames for each test flow (e.g., ["test_login_flow_....json", "test_search_....json"]).
    """
    logger.info(f"Providing resource list of tests from '{TEST_OUTPUT_DIR}'")
    if not os.path.exists(TEST_OUTPUT_DIR) or not os.path.isdir(TEST_OUTPUT_DIR):
        logger.warning(f"Test output directory '{TEST_OUTPUT_DIR}' not found.")
        return []

    try:
        test_files = [
            f for f in os.listdir(TEST_OUTPUT_DIR)
            if os.path.isfile(os.path.join(TEST_OUTPUT_DIR, f)) and f.endswith(".json") and f.startswith(re.sub(r"[ /]", "_", project_directory)) 
        ]
        # Optionally return just the test files, excluding execution results
        test_files = [f for f in test_files if not f.startswith("execution_result_")]
        return sorted(test_files)
    except Exception as e:
        logger.error(f"Error listing test files in '{TEST_OUTPUT_DIR}': {e}", exc_info=True)
        # Re-raise or return empty list? Returning empty is safer for resource.
        return []


@mcp.tool()
def get_security_scan(project_directory: str, target_url: str = None, semgrep_config: str = 'auto') -> Dict[str, Any]:
    """
    Provides a list of vulnerabilities in the code through static code scanning using semgrep, nuclei and zap.
    Also discovers endpoints using ZAP's spider functionality. Try to fix them automatically if you think it is a true positive.

    Args:
    project_directory: The project directory which you want to scan for security issues. Give absolute path only.
    target_url: The target URL for dynamic scanning (ZAP and Nuclei). Required for endpoint discovery.
    semgrep_config: The config for semgrep scans. Default: 'auto'
    
    Returns:
        Dict containing:
        - vulnerabilities: List of vulnerabilities found
        - endpoints: List of discovered endpoints (if target_url provided)
    """
    logging.info("--- Starting Phase 1: Security Scanning ---")
    all_findings = []
    discovered_endpoints = []

    if project_directory:
        # Run Semgrep scan
        logging.info("--- Running Semgrep Scan ---")
        semgrep_findings = run_semgrep(
            code_path=project_directory,
            config=semgrep_config,
            output_dir='./results',
            timeout=600
        )
        if semgrep_findings:
            logging.info(f"Completed Semgrep Scan. Found {len(semgrep_findings)} potential issues.")
            all_findings.extend(semgrep_findings)
        else:
            logging.warning("Semgrep scan completed with no findings or failed.")
            all_findings.append({"Warning": "Semgrep scan completed with no findings or failed."})

        if target_url:
            # First, discover endpoints using ZAP spider
            logging.info("--- Running Endpoint Discovery ---")
            try:
                discovered_endpoints = discover_endpoints(
                    target_url=target_url,
                    output_dir='./results',
                    timeout=600  # 10 minutes for discovery
                )
                logging.info(f"Discovered {len(discovered_endpoints)} endpoints")
            except Exception as e:
                logging.error(f"Error during endpoint discovery: {e}")
                discovered_endpoints = []

            # Run ZAP scan
            logging.info("--- Running ZAP Scan ---")
            try:
                zap_findings = run_zap_scan(
                    target_url=target_url,
                    output_dir='./results',
                    scan_mode="baseline"  # Using baseline scan for quicker results
                )
                if zap_findings and not isinstance(zap_findings[0], str):
                    logging.info(f"Completed ZAP Scan. Found {len(zap_findings)} potential issues.")
                    all_findings.extend(zap_findings)
                else:
                    logging.warning("ZAP scan completed with no findings or failed.")
                    all_findings.append({"Warning": "ZAP scan completed with no findings or failed."})
            except Exception as e:
                logging.error(f"Error during ZAP scan: {e}")
                all_findings.append({"Error": f"ZAP scan failed: {str(e)}"})

            # Run Nuclei scan
            logging.info("--- Running Nuclei Scan ---")
            try:
                nuclei_findings = run_nuclei(
                    target_url=target_url,
                    output_dir='./results'
                )
                if nuclei_findings and not isinstance(nuclei_findings[0], str):
                    logging.info(f"Completed Nuclei Scan. Found {len(nuclei_findings)} potential issues.")
                    all_findings.extend(nuclei_findings)
                else:
                    logging.warning("Nuclei scan completed with no findings or failed.")
                    all_findings.append({"Warning": "Nuclei scan completed with no findings or failed."})
            except Exception as e:
                logging.error(f"Error during Nuclei scan: {e}")
                all_findings.append({"Error": f"Nuclei scan failed: {str(e)}"})
        else:
            logging.info("Skipping dynamic scans and endpoint discovery as target_url was not provided.")

    else:
        logging.info("Skipping scans as project_directory was not provided.")
        all_findings.append({"Warning": "Skipping scans as project_directory was not provided"})

    logging.info("--- Phase 1: Security Scanning Complete ---")
    
    logging.info("--- Starting Phase 2: Consolidating Results ---")
    logging.info(f"Total findings aggregated from all tools: {len(all_findings)}")

    # Save the consolidated report
    consolidated_report_path = save_report(all_findings, "consolidated", './results/', "consolidated_scan_results")

    if consolidated_report_path:
        logging.info(f"Consolidated report saved to: {consolidated_report_path}")
        print(f"\nConsolidated report saved to: {consolidated_report_path}")
    else:
        logging.error("Failed to save the consolidated report.")

    # Save discovered endpoints if any
    if discovered_endpoints:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        endpoints_file = os.path.join('./results', f"discovered_endpoints_{timestamp}.json")
        try:
            with open(endpoints_file, 'w') as f:
                json.dump(discovered_endpoints, f, indent=2)
            logging.info(f"Saved discovered endpoints to: {endpoints_file}")
        except Exception as e:
            logging.error(f"Failed to save endpoints report: {e}")

    logging.info("--- Phase 2: Consolidation Complete ---")
    logging.info("--- Security Automation Script Finished ---")
    
    return {
        "vulnerabilities": all_findings,
        "endpoints": discovered_endpoints
    }


# --- Running the Server ---
# The actual running is handled by `mcp dev` or `mcp install`.
# No `if __name__ == "__main__": mcp.run()` needed here when using the CLI tools.
logger.info("WebTestAgent MCP Server defined. Run with 'mcp dev mcp_server.py'")

if __name__ == "__main__":
    mcp.run()