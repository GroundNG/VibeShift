# nuclei_scanner.py
import logging
import subprocess
import os
import shlex
from datetime import datetime
from .utils import parse_json_file  # Relative import

NUCLEI_TIMEOUT_SECONDS = 900  # 15 minutes default

def run_nuclei(target_url: str, output_dir="results", timeout=NUCLEI_TIMEOUT_SECONDS, severity="low,medium,high,critical"):
    """Runs the Nuclei security scanner against a target URL or IP."""
    if not target_url:
        logging.error("Nuclei target URL/IP is required")
        return []

    logging.info(f"Starting Nuclei scan for target: {target_url}")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"nuclei_output_{timestamp}.json"
    output_filepath = os.path.join(output_dir, output_filename)

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # Configure nuclei command with common best practices
    command = [
        "nuclei", 
        "-target", target_url,
        "-json", 
        "-o", output_filepath,
        "-severity", severity,
        "-silent"
    ]

    logging.debug(f"Executing Nuclei command: {' '.join(shlex.quote(cmd) for cmd in command)}")

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)

        logging.info("Nuclei process finished.")
        logging.debug(f"Nuclei stdout:\n{result.stdout}")

        if result.returncode != 0:
            logging.warning(f"Nuclei exited with non-zero status code: {result.returncode}")
            return [f"Nuclei exited with non-zero status code: {result.returncode}"]

        # Parse the JSON output file
        findings = parse_json_file(output_filepath)
        if findings:
            logging.info(f"Successfully parsed {len(findings)} findings from Nuclei output.")
            # Add tool name for context
            for finding in findings:
                finding['tool'] = 'Nuclei'
                # Standardize some fields to match our expected format
                if 'info' in finding:
                    finding['severity'] = finding.get('info', {}).get('severity')
                    finding['message'] = finding.get('info', {}).get('name')
                    finding['description'] = finding.get('info', {}).get('description')
                    finding['matched_at'] = finding.get('matched-at', '')

            return findings
        else:
            logging.warning(f"Could not parse findings from Nuclei output file: {output_filepath}")
            return [f"Could not parse findings from Nuclei output file: {output_filepath}"]

    except subprocess.TimeoutExpired:
        logging.error(f"Nuclei scan timed out after {timeout} seconds.")
        return [f"Nuclei scan timed out after {timeout} seconds."]
    except FileNotFoundError:
        logging.error("Nuclei command not found. Is Nuclei installed and in PATH?")
        return ["Nuclei command not found. Is Nuclei installed and in PATH?"]
    except Exception as e:
        logging.error(f"An unexpected error occurred while running Nuclei: {e}")
        return [f"An unexpected error occurred while running Nuclei: {e}"]
