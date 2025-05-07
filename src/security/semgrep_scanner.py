# semgrep_scanner.py
import logging
import subprocess
import os
import shlex
from datetime import datetime
from .utils import parse_json_file # Relative import

SEMGREP_TIMEOUT_SECONDS = 600 # 10 minutes default

def run_semgrep(code_path: str, config: str = "auto", output_dir="results", timeout=SEMGREP_TIMEOUT_SECONDS):
    """Runs the Semgrep CLI tool."""
    if not os.path.isdir(code_path):
        logging.error(f"Semgrep target path is not a valid directory: {code_path}")
        return []

    logging.info(f"Starting Semgrep scan for codebase: {code_path} using config: {config}")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"semgrep_output_{timestamp}.json"
    output_filepath = os.path.join(output_dir, output_filename)

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # Use --json for machine-readable output
    command = ["semgrep", "scan", "--config", config, "--json", "-o", output_filepath, code_path]

    logging.debug(f"Executing Semgrep command: {' '.join(shlex.quote(cmd) for cmd in command)}")

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False) # check=False

        logging.info("Semgrep process finished.")
        logging.debug(f"Semgrep stdout:\n{result.stdout}") # Often has progress info
        # if result.stderr:
        #      logging.warning(f"Semgrep stderr:\n{result.stderr}")
        #      return [f"semgrep stderr: \n{result.stderr}"]

        if result.returncode != 0:
            logging.warning(f"Semgrep exited with non-zero status code: {result.returncode}")
            return [f"Semgrep exited with non-zero status code: {result.returncode}"]
            # It might still produce output even with errors (e.g., parse errors)

        # Parse the JSON output file
        report_data = parse_json_file(output_filepath)
        if report_data and "results" in report_data:
             findings = report_data["results"]
             logging.info(f"Successfully parsed {len(findings)} findings from Semgrep output.")
             # Add tool name for context
             for finding in findings:
                 finding['tool'] = 'Semgrep'
                 # Simplify structure slightly if needed
                 finding['message'] = finding.get('extra', {}).get('message')
                 finding['severity'] = finding.get('extra', {}).get('severity')
                 finding['code_snippet'] = finding.get('extra', {}).get('lines')

             return findings
        else:
             logging.warning(f"Could not parse findings from Semgrep output file: {output_filepath}")
             return [f"Could not parse findings from Semgrep output file: {output_filepath}"]

    except subprocess.TimeoutExpired:
        logging.error(f"Semgrep scan timed out after {timeout} seconds.")
        return [f"Semgrep scan timed out after {timeout} seconds."]
    except FileNotFoundError:
        logging.error("Semgrep command not found. Is Semgrep installed and in PATH?")
        return ["Semgrep command not found. Is Semgrep installed and in PATH?"]
    except Exception as e:
        logging.error(f"An unexpected error occurred while running Semgrep: {e}")
        return [f"An unexpected error occurred while running Semgrep: {e}"]