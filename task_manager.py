# task_manager.py
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class TaskManager:
    """Manages the main task, subtasks, progress, and status."""

    def __init__(self, max_retries_per_subtask: int = 2):
        self.main_task: str = ""
        self.subtasks: List[Dict[str, Any]] = []
        # Initialize index to 0, representing the first task to check.
        # Will be updated by get_next_subtask when a task becomes active.
        self.current_subtask_index: int = 0
        self.max_retries_per_subtask: int = max_retries_per_subtask
        logger.info(f"TaskManager initialized (max_retries={max_retries_per_subtask}).")

    def set_main_task(self, task_description: str):
        """Sets the main goal for the agent."""
        self.main_task = task_description
        self.subtasks = []
        self.current_subtask_index = 0 # Reset index when new task is set
        logger.info(f"Main task set: {task_description}")

    def add_subtasks(self, subtask_list: List[str]):
        """Adds a list of subtasks derived from the main task."""
        if not self.main_task:
            logger.error("Cannot add subtasks before setting a main task.")
            return

        if not isinstance(subtask_list, list) or not all(isinstance(s, str) and s for s in subtask_list):
             logger.error(f"Invalid subtask list format received: {subtask_list}")
             raise ValueError("Subtask list must be a non-empty list of non-empty strings.")

        self.subtasks = [] # Clear existing subtasks before adding new ones
        for desc in subtask_list:
            self.subtasks.append({
                "description": desc,
                "status": "pending",  # pending, in_progress, done, failed
                "attempts": 0,
                "result": None,
                "error": None,
                "last_failed_selector": None # Added field
            })
        # Reset index to the start of the new list
        self.current_subtask_index = 0 if self.subtasks else -1
        logger.info(f"Added {len(subtask_list)} subtasks.")


    def get_next_subtask(self) -> Optional[Dict[str, Any]]:
        """
        Gets the first subtask that is 'pending' or 'failed' with retries remaining.
        Iterates through the list sequentially from the beginning.
        """
        # Iterate through all tasks from the beginning
        for index, task in enumerate(self.subtasks):
            # Check if the task is actionable
            is_pending = task["status"] == "pending"
            is_retryable_failure = (task["status"] == "failed" and
                                    task["attempts"] < self.max_retries_per_subtask)

            if is_pending or is_retryable_failure:
                 # Found the next actionable task

                 # Log appropriately
                 if is_retryable_failure:
                     logger.info(f"Retrying subtask {index + 1} (Attempt {task['attempts'] + 1}/{self.max_retries_per_subtask})")
                     # Ensure error from previous attempt is preserved in the task dict
                 else: # It's pending
                      logger.info(f"Starting subtask {index + 1}/{len(self.subtasks)}: {task['description']}")

                 # Update the main index to point to this task
                 self.current_subtask_index = index

                 # Mark as in_progress and increment attempt count
                 task["status"] = "in_progress"
                 task["attempts"] += 1
                 # Clear previous error/result before returning for execution
                 # task["error"] = None # Keep error for context on retry
                 # task["result"] = None
                 return task # Return the task to be executed

            # If task status is 'done', 'in_progress' (shouldn't happen if logic is correct),
            # or 'failed' without retries, continue to the next task in the list.

        # If the loop completes without finding an actionable task
        logger.info("No more actionable subtasks found. All tasks are done or failed permanently.")
        self.current_subtask_index = len(self.subtasks) # Mark completion by setting index out of bounds
        return None


    def update_subtask_status(self, index: int, status: str, result: Any = None, error: Optional[str] = None, force_update: bool = False):
        if 0 <= index < len(self.subtasks):
            task = self.subtasks[index]

            # Allow update if forced OR if task is in_progress
            if not force_update and task["status"] != "in_progress":
                logger.warning(f"Attempted to update status of subtask {index + 1} ('{task['description'][:50]}...') "
                            f"from '{task['status']}' to '{status}'. Ignoring update as task is not 'in_progress' and force_update=False.")
                return

            # Proceed with update
            task["status"] = status
            task["result"] = result # Store success result (or intermediate data)
            task["error"] = error   # Store error message on failure
            # logger.info("My info: ", self.main_task, ":::::", str(self.subtasks))
            logger.info(f"MY info : {self.main_task} :::: {self.subtasks}")
            log_message = f"Subtask {index + 1} ('{task['description'][:50]}...') status updated to: {status}."
            if result: log_message += f" Result: {str(result)[:100]}..."
            if error: log_message += f" Error: {error}"
            logger.info(log_message)

            # If task failed permanently, log a warning
            if status == "failed" and task["attempts"] >= self.max_retries_per_subtask:
                 logger.warning(f"Subtask {index + 1} failed permanently after {task['attempts']} attempts.")

            # ---- REMOVED index advancement logic ----
            # get_next_subtask() is now solely responsible for finding the next task.

        else:
            logger.error(f"Invalid index {index} for updating subtask status (Total tasks: {len(self.subtasks)}).")


    def get_current_subtask(self) -> Optional[Dict[str, Any]]:
         """Gets the subtask currently marked by current_subtask_index."""
         # Check if index is valid *before* accessing
         if 0 <= self.current_subtask_index < len(self.subtasks):
              # Check if the task at this index is actually the one 'in_progress'
              task = self.subtasks[self.current_subtask_index]
              # This might return a task that just finished, which is okay for context
              return task
         # If index is out of bounds (e.g., after completion), return None
         elif self.current_subtask_index >= len(self.subtasks):
              logger.debug("get_current_subtask: Index indicates completion.")
              return None
         else: # Should not happen if logic is correct (-1 initialization case?)
              logger.warning(f"get_current_subtask: current_subtask_index ({self.current_subtask_index}) is invalid, but not yet indicating completion.")
              return None # Or potentially return the first task if index is -1?


    def is_complete(self) -> bool:
        """Checks if the agent has processed all subtasks (either done or failed permanently)."""
        # More robust check: Iterate through tasks. If any are still actionable, not complete.
        for task in self.subtasks:
            if task['status'] == 'pending' or \
               task['status'] == 'in_progress' or \
               (task['status'] == 'failed' and task['attempts'] < self.max_retries_per_subtask):
                return False # Found an actionable task
        # If loop finishes without finding actionable tasks, it's complete.
        return True


    def get_progress_summary(self) -> str:
        if not self.main_task:
            return "No task set."

        summary = f"Main Task: {self.main_task}\n"
        total = len(self.subtasks)
        if total == 0:
            return summary + "No subtasks defined yet."

        # Recalculate counts directly from the list each time for accuracy
        done = sum(1 for t in self.subtasks if t['status'] == 'done')
        perm_failed = sum(1 for t in self.subtasks if t['status'] == 'failed' and t['attempts'] >= self.max_retries_per_subtask)
        # Note: 'retrying' isn't a status. We check 'failed' with attempts < max.
        failed_retryable = sum(1 for t in self.subtasks if t['status'] == 'failed' and t['attempts'] < self.max_retries_per_subtask)
        in_progress = sum(1 for t in self.subtasks if t['status'] == 'in_progress')
        pending = sum(1 for t in self.subtasks if t['status'] == 'pending')

        summary += f"Progress: {done} Done | {perm_failed} Failed (Perm.) | {failed_retryable} Failed (Retryable) | {in_progress} In Progress | {pending} Pending (Total: {total}).\n"

        # Try to get the task currently being worked on (or just finished)
        active_task = None
        if 0 <= self.current_subtask_index < len(self.subtasks):
             active_task = self.subtasks[self.current_subtask_index]

        if active_task and active_task['status'] == 'in_progress':
             status_detail = f"In Progress (Attempt {active_task['attempts']})"
             summary += f"Current Subtask (#{self.current_subtask_index + 1} - {status_detail}): {active_task['description']}"
             if active_task.get('error'): # Show error if retrying
                  summary += f"\n  Last Error: {active_task['error']}"
        elif self.is_complete():
             summary += "All subtasks processed."
        else:
             # Find the index of the *next* task that *will* be processed
             next_idx = -1
             for i, task in enumerate(self.subtasks):
                 if task['status'] == 'pending' or (task['status'] == 'failed' and task['attempts'] < self.max_retries_per_subtask):
                     next_idx = i
                     break
             if next_idx != -1:
                 summary += f"Next Subtask (#{next_idx + 1}): {self.subtasks[next_idx]['description']}"
             else: # Should mean complete, but is_complete() handles this
                 summary += "Processing complete or unexpected state."


        return summary