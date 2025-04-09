# server.py
import sys
import os
import logging
import warnings
import asyncio
import multiprocessing as mp # Import multiprocessing

# Ensure the ai_web_agent package is findable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '.')))

from fastapi import FastAPI, HTTPException, status # Added status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from typing import Optional, Any, Dict
from queue import Empty as QueueEmpty # For checking queue result

# Import agent components AFTER setting sys.path
from agent import WebAgent
from llm_client import GeminiClient
from utils import load_api_key
import uvicorn

# --- Configuration, Logging, Warnings (remain the same) ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AgentAPI")
HEADLESS_BROWSER = True
MAX_AGENT_ITERATIONS = 40
MAX_HISTORY = 8
warnings.warn(
    "SECURITY WARNING: This API runs an AI agent that interacts with the live web. "
    "Ensure API access is secured and commands are vetted. "
    "Use with extreme caution.",
    UserWarning
)
logger.warning("*" * 70)
logger.warning("!!! API SECURITY WARNING !!!")
logger.warning("Agent interacts with live websites based on API input.")
logger.warning("Ensure proper authentication/authorization is implemented if deployed.")
logger.warning("*" * 70)
# --- End Security Warning ---

# --- FastAPI App Setup ---
app = FastAPI(
    title="AI Web Agent API",
    description="API to control an AI agent for web browsing tasks.",
    version="0.1.0"
)

# --- Request/Response Models (remain the same) ---
class InteractRequest(BaseModel):
    command: str
    headless: Optional[bool] = None

class InteractResponse(BaseModel):
    success: bool
    message: str
    output_file: Optional[str] = None
    final_answer: Optional[str] = None
    duration_seconds: float
    task_summary: str

class PlaceholderResponse(BaseModel):
    message: str
# --- End Models ---


# --- Target function for the separate process ---
# IMPORTANT: This function runs in a completely separate process.
# It cannot directly share memory with the FastAPI process (except via Queue/Pipe etc.)
# It also needs to handle its own imports and initializations if necessary,
# although importing top-level modules like WebAgent usually works fine.
def run_agent_process_target(command: str, run_headless: bool, result_queue: mp.Queue):
    """
    Initializes and runs the WebAgent in a separate process, putting the
    result dictionary into the provided multiprocessing Queue.
    """
    # Setup logging within the process if needed (optional, can be complex)
    # basicConfig might conflict if called multiple times, consider specific handlers
    process_logger = logging.getLogger(f"AgentProcess-{os.getpid()}")
    # Add basic handler to see output from the process if needed:
    # handler = logging.StreamHandler(sys.stdout)
    # formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    # handler.setFormatter(formatter)
    # process_logger.addHandler(handler)
    # process_logger.setLevel(logging.INFO)

    agent_instance = None
    result = {}
    try:
        process_logger.info(f"Process started. Command: '{command}' (Headless: {run_headless})")
        api_key = load_api_key() # Load API key within the process
        gemini_client = GeminiClient(api_key=api_key)

        # Ensure output dir exists (agent might also do this)
        if not os.path.exists("output"):
            try:
                os.makedirs("output")
                process_logger.info("Created 'output' directory.")
            except OSError as e:
                process_logger.warning(f"Could not create 'output' directory: {e}")

        # NOTE: Creates a NEW WebAgent instance in this separate process
        agent_instance = WebAgent(
            gemini_client=gemini_client,
            headless=run_headless,
            max_iterations=MAX_AGENT_ITERATIONS,
            max_history_length=MAX_HISTORY
        )

        # Run the agent (this is the synchronous call, safe in this process)
        result = agent_instance.run(command) # agent.run already returns a dict
        process_logger.info("Agent run finished in process.")

    except Exception as e:
        process_logger.exception(f"Critical error during agent execution in process for command: '{command}'")
        # Create a consistent error dictionary
        result = {
            "success": False,
            "message": f"Agent execution failed with critical error in process: {e}",
            "output_file": None,
            "final_answer": None,
            "duration_seconds": 0.0,
            "task_summary": "Run failed critically in process.",
        }
    finally:
        # Ensure the result (even if it's an error dict) is put onto the queue
        try:
            result_queue.put(result)
            process_logger.info("Result placed onto queue.")
        except Exception as qe:
            # Log if putting result on queue fails (less likely)
            process_logger.exception(f"Failed to put result onto queue: {qe}")
        # IMPORTANT: Browser cleanup happens within agent_instance.run()'s finally block


# --- Helper function to wait for process and get result (Blocking) ---
# This function contains blocking calls (join, get) and will be run
# in FastAPI's threadpool to avoid blocking the main asyncio loop.
def _wait_for_process_and_get_result(process: mp.Process, queue: mp.Queue, timeout_secs: int = 300) -> Dict[str, Any]:
    """Waits for the process to finish and retrieves the result from the queue."""
    logger.debug(f"Waiting for agent process {process.pid} to complete...")
    process.join(timeout=timeout_secs) # Wait for the process to finish with a timeout

    if process.is_alive():
        logger.error(f"Agent process {process.pid} timed out after {timeout_secs}s. Terminating.")
        process.terminate() # Forcefully terminate if it hangs
        process.join(timeout=5) # Wait a bit longer for termination
        return {
             "success": False,
             "message": f"Agent process timed out after {timeout_secs} seconds and was terminated.",
             "output_file": None, "final_answer": None, "duration_seconds": timeout_secs, "task_summary": "Process timed out."
        }

    logger.debug(f"Agent process {process.pid} finished. Exit code: {process.exitcode}")
    if process.exitcode != 0:
        # Log if the process exited abnormally, but still try to get result from queue
        logger.warning(f"Agent process {process.pid} exited with non-zero code: {process.exitcode}")

    try:
        # Get the result from the queue (use timeout to prevent indefinite block)
        result = queue.get(timeout=10)
        logger.info(f"Retrieved result from queue for process {process.pid}")
        return result
    except QueueEmpty:
        logger.error(f"Agent process {process.pid} finished (exit code {process.exitcode}), but no result found in the queue.")
        return {
             "success": False,
             "message": f"Agent process finished (exit code {process.exitcode}) but failed to return a result via the queue.",
             "output_file": None, "final_answer": None, "duration_seconds": 0.0, "task_summary": "Process finished, result missing."
         }
    except Exception as e:
        logger.exception(f"Error retrieving result from queue for process {process.pid}: {e}")
        return {
            "success": False,
            "message": f"Error retrieving result from queue: {e}",
            "output_file": None, "final_answer": None, "duration_seconds": 0.0, "task_summary": "Error getting result from queue."
        }
    finally:
         queue.close() # Clean up the queue


# --- API Endpoints ---
@app.post("/interact", response_model=InteractResponse)
async def interact_with_agent(request: InteractRequest):
    """
    Accepts a natural language command, runs the AI web agent **in a separate process**,
    and returns the final status or result.
    """
    logger.info(f"Received /interact request: command='{request.command}'")

    run_headless = request.headless if request.headless is not None else HEADLESS_BROWSER

    # Use a multiprocessing context for potentially cleaner startup/shutdown
    # Although default context is usually fine
    ctx = mp.get_context('spawn') # 'spawn' is often more stable across platforms than 'fork'

    result_queue = ctx.Queue()
    agent_process = ctx.Process(
        target=run_agent_process_target,
        args=(request.command, run_headless, result_queue),
        daemon=True # Set as daemon so it doesn't block exit if main process quits unexpectedly
    )

    try:
        logger.info(f"Starting agent process for command: '{request.command}'")
        agent_process.start()
        logger.info(f"Agent process {agent_process.pid} started.")

        # Use run_in_threadpool to wait for the process completion
        # and get the result from the queue without blocking FastAPI's event loop.
        # Increase timeout as agent runs can be long
        process_result = await run_in_threadpool(_wait_for_process_and_get_result, agent_process, result_queue, timeout_secs=600) # 10 min timeout

        # Check the structure of the result
        if not isinstance(process_result, dict) or "success" not in process_result:
             logger.error(f"Invalid result received from agent process {agent_process.pid}: {process_result}")
             raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Agent process returned an invalid result format.")

        # If the process failed internally or timed out, return an appropriate HTTP error
        if not process_result["success"]:
             logger.warning(f"Agent process {agent_process.pid} reported failure: {process_result.get('message')}")
             # Return 500 for internal failures, maybe 504 for timeout? Let's use 500 for now.
             raise HTTPException(
                 status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                 detail=process_result.get('message', 'Agent process reported an unspecified failure.')
             )

        # Process succeeded, return the result
        logger.info(f"Agent process {agent_process.pid} completed successfully.")
        return InteractResponse(**process_result)

    except Exception as e:
        # Catch errors related to starting the process or handling the result
        logger.exception("Error processing /interact request")
        # Ensure process is cleaned up if it's still running after an error here
        if agent_process.is_alive():
            logger.warning(f"Terminating agent process {agent_process.pid} due to FastAPI error.")
            agent_process.terminate()
            agent_process.join(timeout=5)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Internal server error processing agent request: {e}")
    finally:
        # Ensure queue is closed even if errors occur in FastAPI handling
        # (also closed in _wait_for_process_and_get_result, but belt-and-suspenders)
        result_queue.close()
        result_queue.join_thread() # Wait for queue background thread


# --- Other endpoints (/extract, /) remain the same ---
@app.post("/extract", response_model=PlaceholderResponse)
async def extract_data():
    logger.info("Received /extract request (Placeholder)")
    return PlaceholderResponse(message="Extraction endpoint is not yet implemented.")

@app.get("/", response_model=PlaceholderResponse)
async def root():
    return PlaceholderResponse(message="AI Web Agent API is running. Use /interact or /extract endpoints.")

# --- Run the Server (remains the same) ---
if __name__ == "__main__":
    # Required for multiprocessing to work correctly on Windows and macOS 'spawn' context
    mp.freeze_support() # Add this line
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)