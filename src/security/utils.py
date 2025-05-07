# utils.py
import logging
import json
import os
from datetime import datetime

LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'

def setup_logging(log_level=logging.INFO):
    """Configures basic logging."""
    logging.basicConfig(level=log_level, format=LOG_FORMAT)

def save_report(data, tool_name, output_dir="results", filename_prefix="report"):
    """Saves the collected data to a JSON file."""
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{filename_prefix}_{tool_name}_{timestamp}.json"
    filepath = os.path.join(output_dir, filename)

    try:
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=4)
        logging.info(f"Successfully saved {tool_name} report to {filepath}")
        return filepath
    except Exception as e:
        logging.error(f"Failed to save {tool_name} report to {filepath}: {e}")
        return None

def parse_json_lines_file(filepath):
    """Parses a file containing JSON objects, one per line."""
    results = []
    if not os.path.exists(filepath):
        logging.error(f"File not found for parsing: {filepath}")
        return results
    try:
        with open(filepath, 'r') as f:
            for line in f:
                try:
                    if line.strip():
                        results.append(json.loads(line))
                except json.JSONDecodeError as e:
                    logging.warning(f"Skipping invalid JSON line in {filepath}: {line.strip()} - Error: {e}")
        return results
    except Exception as e:
        logging.error(f"Failed to read or parse JSON lines file {filepath}: {e}")
        return [] # Return empty list on failure

def parse_json_file(filepath):
    """Parses a standard JSON file."""
    if not os.path.exists(filepath):
        logging.error(f"File not found for parsing: {filepath}")
        return None
    try:
        with open(filepath, 'r') as f:
            data = json.load(f)
        return data
    except json.JSONDecodeError as e:
        logging.error(f"Invalid JSON format in {filepath}: {e}")
        return None
    except Exception as e:
        logging.error(f"Failed to read or parse JSON file {filepath}: {e}")
        return None