# task_manager.py
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class TaskManager:
    """Manages the main task, subtasks, progress, and status."""

    def __init__(self, max_retries_per_subtask: int = 2): # Renamed parameter for clarity internally
        self.main_task: str = "" # Stores the overall feature description
        self.subtasks: List[Dict[str, Any]] = [] # Stores the individual test steps
        self.current_subtask_index: int = 0 # Index of the step being processed or next to process
        self.max_retries_per_subtask: int = max_retries_per_subtask
        logger.info(f"TaskManager (Test Mode) initialized (max_retries_per_step={max_retries_per_subtask}).")

    def set_main_task(self, feature_description: str):
        """Sets the main feature description being tested."""
        self.main_task = feature_description
        self.subtasks = []
        self.current_subtask_index = 0
        logger.info(f"Feature under test set: {feature_description}")


    def add_subtasks(self, test_step_list: List[str]):
        """Adds a list of test steps derived from the feature description."""
        if not self.main_task:
            logger.error("Cannot add test steps before setting a feature description.")
            return

        if not isinstance(test_step_list, list) or not all(isinstance(s, str) and s for s in test_step_list):
             logger.error(f"Invalid test step list format received: {test_step_list}")
             raise ValueError("Test step list must be a non-empty list of non-empty strings.")

        self.subtasks = [] # Clear existing steps before adding new ones
        for desc in test_step_list:
            self.subtasks.append({
                "description": desc, # The test step description
                "status": "pending",  # pending, in_progress, done, failed
                "attempts": 0,
                "result": None, # Store result of the step (e.g., extracted text)
                "error": None,  # Store error if the step failed
                "_recorded_": False,
                "last_failed_selector": None # Store selector if failure was element-related
            })
        self.current_subtask_index = 0 if self.subtasks else -1 # Reset index
        logger.info(f"Added {len(test_step_list)} test steps.")




    def get_next_subtask(self) -> Optional[Dict[str, Any]]:
        """
        Gets the first test step that is 'pending' or 'failed' with retries remaining.
        Iterates sequentially.
        """
        for index, task in enumerate(self.subtasks):
            # In recorder mode, 'failed' means AI suggestion failed, allow retry
            # In executor mode (if used here), 'failed' means execution failed
            is_pending = task["status"] == "pending"
            is_retryable_failure = (task["status"] == "failed" and
                                    task["attempts"] <= self.max_retries_per_subtask)

            if is_pending or is_retryable_failure:
                 # Found the next actionable step

                 if is_retryable_failure:
                     logger.info(f"Retrying test step {index + 1} (Attempt {task['attempts'] + 1}/{self.max_retries_per_subtask + 1})")
                 else: # Pending
                      logger.info(f"Starting test step {index + 1}/{len(self.subtasks)}: {task['description']}")

                 # Update the main index to point to this task BEFORE changing status
                 self.current_subtask_index = index

                 task["status"] = "in_progress"
                 task["attempts"] += 1
                 # Keep error context on retry, clear result
                 task["result"] = None
                 return task

        # No actionable tasks found
        logger.info("No more actionable test steps found.")
        self.current_subtask_index = len(self.subtasks) # Mark completion
        return None



    def update_subtask_status(self, index: int, status: str, result: Any = None, error: Optional[str] = None, force_update: bool = False):
        """Updates the status of a specific test step."""
        if 0 <= index < len(self.subtasks):
            task = self.subtasks[index]
            current_status = task["status"]
            # Allow update only if forced or if task is 'in_progress'
            # if not force_update and task["status"] != "in_progress":
            #     logger.warning(f"Attempted to update status of test step {index + 1} ('{task['description'][:50]}...') "
            #                 f"from '{task['status']}' to '{status}', but it's not 'in_progress'. Ignoring (unless force_update=True).")
            #     return
            
            # Log if the status is actually changing
            if current_status != status:
                logger.info(f"Updating Test Step {index + 1} status from '{current_status}' to '{status}'.")
            else:
                 logger.debug(f"Test Step {index + 1} status already '{status}'. Updating result/error.")

            task["status"] = status
            task["result"] = result
            task["error"] = error

            log_message = f"Test Step {index + 1} ('{task['description'][:50]}...') processed. Status: {status}."
            if result and status == 'done': log_message += f" Result: {str(result)[:100]}..."
            if error: log_message += f" Error/Note: {error}"
            # Use debug for potentially repetitive updates if status doesn't change
            log_level = logging.INFO if current_status != status else logging.DEBUG
            logger.log(log_level, log_message)

            # Log permanent failure clearly
            if status == "failed" and task["attempts"] > self.max_retries_per_subtask:
                 logger.warning(f"Test Step {index + 1} failed permanently after {task['attempts']} attempts.")

        else:
            logger.error(f"Invalid index {index} for updating test step status (Total steps: {len(self.subtasks)}).")



    def get_current_subtask(self) -> Optional[Dict[str, Any]]:
         """Gets the test step currently marked by current_subtask_index (likely 'in_progress')."""
         if 0 <= self.current_subtask_index < len(self.subtasks):
              return self.subtasks[self.current_subtask_index]
         return None



    def is_complete(self) -> bool:
        """Checks if all test steps have been processed (are 'done' or 'failed' permanently)."""
        for task in self.subtasks:
            if task['status'] == 'pending' or \
               task['status'] == 'in_progress' or \
               (task['status'] == 'failed' and task['attempts'] <= self.max_retries_per_subtask):
                return False # Found an actionable step
        return True # All steps processed



    def get_progress_summary(self) -> str:
        """Generates a summary of the test steps progress."""
        if not self.main_task:
            return "No feature description set."

        summary = f"Feature: {self.main_task}\n"
        total = len(self.subtasks)
        if total == 0:
            return summary + "No test steps defined yet."

        done = sum(1 for t in self.subtasks if t['status'] == 'done')
        skipped = sum(1 for t in self.subtasks if t['status'] == 'skipped')
        # Failed permanently means status is 'failed' AND attempts > max_retries
        perm_failed = sum(1 for t in self.subtasks if t['status'] == 'failed' and t['attempts'] > self.max_retries_per_subtask)
        # Failed with retries means status is 'failed' AND attempts <= max_retries
        failed_retryable = sum(1 for t in self.subtasks if t['status'] == 'failed' and t['attempts'] <= self.max_retries_per_subtask)
        in_progress = sum(1 for t in self.subtasks if t['status'] == 'in_progress')
        pending = sum(1 for t in self.subtasks if t['status'] == 'pending')

        # Recalculate 'completed' for the summary string (done + perm_failed)
        completed_count = done + perm_failed
        summary += f"Progress Summary: {done} Done | {skipped} Skipped |  {perm_failed} Failed (Perm.) | {failed_retryable} Failed (Retryable) | {in_progress} In Progress | {pending} Pending (Total: {total})" 

        # Find the current or next step for detailed status
        current_or_next_idx = -1
        status_detail = ""
        active_task = None

        if 0 <= self.current_subtask_index < len(self.subtasks):
             task = self.subtasks[self.current_subtask_index]
             if task['status'] == 'in_progress':
                  current_or_next_idx = self.current_subtask_index
                  status_detail = f"In Progress (Attempt {task['attempts']})"
                  active_task = task
             elif task['status'] == 'pending' or (task['status'] == 'failed' and task['attempts'] <= self.max_retries_per_subtask):
                  current_or_next_idx = self.current_subtask_index
                  status_detail = f"Next Step ({task['status']})"
                  active_task = task

        # If not currently in progress, find the *next* pending or retryable
        if active_task is None:
             for i, task in enumerate(self.subtasks):
                  if task['status'] == 'pending' or (task['status'] == 'failed' and task['attempts'] <= self.max_retries_per_subtask):
                       current_or_next_idx = i
                       status_detail = f"Next Step ({task['status']})"
                       active_task = task
                       break

        if active_task:
            summary += f"\nCurrent/Next Step (#{current_or_next_idx + 1} - {status_detail}): {active_task['description']}"
            if active_task.get('error'): # Show last error if retrying or just failed
                 summary += f"\n  Last Error: {active_task['error']}"
        elif self.is_complete():
             # Check final outcome
             if all(t['status'] == 'done' for t in self.subtasks):
                  summary += "\nStatus: All test steps completed successfully."
             else:
                  failed_count = sum(1 for t in self.subtasks if t['status'] == 'failed' and t['attempts'] > self.max_retries_per_subtask)
                  summary += f"\nStatus: Test finished. {failed_count} step(s) failed permanently."

        else:
             # This case should ideally not be reached if logic is sound
             summary += "\nStatus: Test steps processing status unclear."


        return summary
