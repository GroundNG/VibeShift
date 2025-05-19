# zap_scanner.py
import logging
import subprocess
import os
import shlex
import time
import json
import requests
from datetime import datetime
from .utils import parse_json_file  # Relative import

ZAP_TIMEOUT_SECONDS = 1800  # 30 minutes default
ZAP_API_PORT = 8080  # Default ZAP API port

def run_zap_scan(target_url: str, output_dir="results", timeout=ZAP_TIMEOUT_SECONDS, 
                 zap_path=None, api_key=None, scan_mode="baseline"):
    """
    Runs OWASP ZAP security scanner against a target URL.
    
    Args:
        target_url: The URL to scan
        output_dir: Directory to store scan results
        timeout: Maximum time in seconds for the scan
        zap_path: Path to ZAP installation (uses docker by default)
        api_key: ZAP API key if required
        scan_mode: Type of scan - 'baseline', 'full' or 'api'
    """
    if not target_url:
        logging.error("ZAP target URL is required")
        return []

    logging.info(f"Starting ZAP scan for target: {target_url}")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"zap_output_{timestamp}.json"
    output_filepath = os.path.join(output_dir, output_filename)

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # Determine if using Docker or local ZAP installation
    use_docker = zap_path is None
    
    if use_docker:
        # Docker command to run ZAP in a container
        command = [
            "docker", "run", "--rm", "-v", f"{os.path.abspath(output_dir)}:/zap/wrk:rw",
            "-t", "owasp/zap2docker-stable", "zap-" + scan_mode + "-scan.py",
            "-t", target_url,
            "-J", output_filename
        ]
        
        if api_key:
            command.extend(["-z", f"api.key={api_key}"])
    else:
        # Local ZAP installation
        script_name = f"zap-{scan_mode}-scan.py"
        command = [
            os.path.join(zap_path, script_name),
            "-t", target_url,
            "-J", output_filepath
        ]
        
        if api_key:
            command.extend(["-z", f"api.key={api_key}"])

    logging.debug(f"Executing ZAP command: {' '.join(shlex.quote(cmd) for cmd in command)}")

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)

        logging.info("ZAP process finished.")
        logging.debug(f"ZAP stdout:\n{result.stdout}")

        if result.returncode != 0:
            logging.warning(f"ZAP exited with non-zero status code: {result.returncode}")
            return [f"ZAP exited with non-zero status code: {result.returncode}"]

        # For Docker, the output will be in the mapped volume
        actual_output_path = output_filepath if not use_docker else os.path.join(output_dir, output_filename)
        
        # Parse the JSON output file
        report_data = parse_json_file(actual_output_path)
        
        if report_data and "site" in report_data:
            # Process ZAP findings from the report
            findings = []
            
            # Structure varies based on scan mode but generally has sites with alerts
            for site in report_data.get("site", []):
                site_url = site.get("@name", "")
                for alert in site.get("alerts", []):
                    finding = {
                        'tool': 'OWASP ZAP',
                        'severity': alert.get("riskdesc", "").split(" ", 1)[0],
                        'message': alert.get("name", ""),
                        'description': alert.get("desc", ""),
                        'url': site_url,
                        'solution': alert.get("solution", ""),
                        'references': alert.get("reference", ""),
                        'cweid': alert.get("cweid", ""),
                        'instances': len(alert.get("instances", [])),
                    }
                    findings.append(finding)
            
            logging.info(f"Successfully parsed {len(findings)} findings from ZAP output.")
            return findings
        else:
            logging.warning(f"Could not parse findings from ZAP output file: {actual_output_path}")
            return [f"Could not parse findings from ZAP output file: {actual_output_path}"]

    except subprocess.TimeoutExpired:
        logging.error(f"ZAP scan timed out after {timeout} seconds.")
        return [f"ZAP scan timed out after {timeout} seconds."]
    except FileNotFoundError as e:
        if use_docker:
            logging.error("Docker command not found. Is Docker installed and in PATH?")
            return ["Docker command not found. Is Docker installed and in PATH?"]
        else:
            logging.error(f"ZAP command not found at {zap_path}. Is ZAP installed?")
            return [f"ZAP command not found at {zap_path}. Is ZAP installed?"]
    except Exception as e:
        logging.error(f"An unexpected error occurred while running ZAP: {e}")
        return [f"An unexpected error occurred while running ZAP: {e}"]


def run_zap_api_scan(target_url: str, api_definition: str, output_dir="results", 
                    timeout=ZAP_TIMEOUT_SECONDS, zap_path=None, api_key=None):
    """
    Runs ZAP API scan against a REST API with OpenAPI/Swagger definition.
    
    Args:
        target_url: Base URL of the API
        api_definition: Path to OpenAPI/Swagger definition file
        output_dir: Directory to store scan results
        timeout: Maximum time in seconds for the scan
        zap_path: Path to ZAP installation (uses docker by default)
        api_key: ZAP API key if required
    """
    if not os.path.isfile(api_definition):
        logging.error(f"API definition file not found: {api_definition}")
        return []
        
    # Similar implementation as run_zap_scan but with API scanning options
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"zap_api_output_{timestamp}.json"
    output_filepath = os.path.join(output_dir, output_filename)

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    use_docker = zap_path is None
    
    if use_docker:
        # Volume mount for API definition file
        api_def_dir = os.path.dirname(os.path.abspath(api_definition))
        api_def_file = os.path.basename(api_definition)
        
        command = [
            "docker", "run", "--rm", 
            "-v", f"{os.path.abspath(output_dir)}:/zap/wrk:rw",
            "-v", f"{api_def_dir}:/zap/api:ro",
            "-t", "owasp/zap2docker-stable", "zap-api-scan.py",
            "-t", target_url,
            "-f", f"/zap/api/{api_def_file}",
            "-J", output_filename
        ]
    else:
        command = [
            os.path.join(zap_path, "zap-api-scan.py"),
            "-t", target_url,
            "-f", api_definition,
            "-J", output_filepath
        ]
    
    if api_key:
        command.extend(["-z", f"api.key={api_key}"])
    
    logging.debug(f"Executing ZAP API scan command: {' '.join(shlex.quote(cmd) for cmd in command)}")
    
    # The rest of the implementation follows similar pattern to run_zap_scan
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        
        # Processing similar to run_zap_scan
        logging.info("ZAP API scan process finished.")
        logging.debug(f"ZAP stdout:\n{result.stdout}")

        if result.returncode != 0:
            logging.warning(f"ZAP API scan exited with non-zero status code: {result.returncode}")
            return [f"ZAP API scan exited with non-zero status code: {result.returncode}"]

        # For Docker, the output will be in the mapped volume
        actual_output_path = output_filepath if not use_docker else os.path.join(output_dir, output_filename)
        
        # Parse the JSON output file - same processing as run_zap_scan
        report_data = parse_json_file(actual_output_path)
        
        if report_data and "site" in report_data:
            findings = []
            
            for site in report_data.get("site", []):
                site_url = site.get("@name", "")
                for alert in site.get("alerts", []):
                    finding = {
                        'tool': 'OWASP ZAP API Scan',
                        'severity': alert.get("riskdesc", "").split(" ", 1)[0],
                        'message': alert.get("name", ""),
                        'description': alert.get("desc", ""),
                        'url': site_url,
                        'solution': alert.get("solution", ""),
                        'references': alert.get("reference", ""),
                        'cweid': alert.get("cweid", ""),
                        'instances': len(alert.get("instances", [])),
                    }
                    findings.append(finding)
            
            logging.info(f"Successfully parsed {len(findings)} findings from ZAP API scan output.")
            return findings
        else:
            logging.warning(f"Could not parse findings from ZAP API scan output file: {actual_output_path}")
            return [f"Could not parse findings from ZAP API scan output file: {actual_output_path}"]
            
    except Exception as e:
        logging.error(f"An unexpected error occurred while running ZAP API scan: {e}")
        return [f"An unexpected error occurred while running ZAP API scan: {e}"]

def discover_endpoints(target_url: str, output_dir="results", timeout=600, zap_path=None, api_key=None):
    """
    Uses ZAP's spider to discover endpoints in a web application.
    
    Args:
        target_url: The URL to scan
        output_dir: Directory to store results
        timeout: Maximum time in seconds for the spider
        zap_path: Path to ZAP installation (uses docker by default)
        api_key: ZAP API key if required
    
    Returns:
        List of discovered endpoints and their details
    """
    if not target_url:
        logging.error("Target URL is required for endpoint discovery")
        return []

    logging.info(f"Starting ZAP endpoint discovery for: {target_url}")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"zap_endpoints_{timestamp}.json"
    output_filepath = os.path.join(output_dir, output_filename)

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    use_docker = zap_path is None
    
    if use_docker:
        command = [
            "docker", "run", "--rm",
            "-v", f"{os.path.abspath(output_dir)}:/zap/wrk:rw",
            "-t", "owasp/zap2docker-stable",
            "zap-full-scan.py",
            "-t", target_url,
            "-J", output_filename,
            "-z", "-config spider.maxDuration=1",  # Limit spider duration
            "--spider-first",  # Run spider before the scan
            "-n", "endpoints.context"  # Don't perform actual scan, just spider
        ]
    else:
        command = [
            os.path.join(zap_path, "zap-full-scan.py"),
            "-t", target_url,
            "-J", output_filepath,
            "-z", "-config spider.maxDuration=1",
            "--spider-first",
            "-n", "endpoints.context"
        ]

    if api_key:
        command.extend(["-z", f"api.key={api_key}"])

    logging.debug(f"Executing ZAP endpoint discovery: {' '.join(shlex.quote(cmd) for cmd in command)}")

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        
        logging.info("ZAP endpoint discovery finished.")
        logging.debug(f"ZAP stdout:\n{result.stdout}")

        actual_output_path = output_filepath if not use_docker else os.path.join(output_dir, output_filename)
        
        # Parse the JSON output file
        report_data = parse_json_file(actual_output_path)
        
        if report_data:
            endpoints = []
            # Extract endpoints from spider results
            if "site" in report_data:
                for site in report_data.get("site", []):
                    site_url = site.get("@name", "")
                    # Extract URLs from alerts and spider results
                    urls = set()
                    
                    # Get URLs from alerts
                    for alert in site.get("alerts", []):
                        for instance in alert.get("instances", []):
                            url = instance.get("uri", "")
                            if url:
                                urls.add(url)
                    
                    # Add discovered endpoints
                    for url in urls:
                        endpoint = {
                            'url': url,
                            'method': 'GET',  # Default to GET, ZAP spider mainly discovers GET endpoints
                            'source': 'ZAP Spider',
                            'parameters': [],  # Could be enhanced to parse URL parameters
                            'discovered_at': datetime.now().isoformat()
                        }
                        endpoints.append(endpoint)
            
            logging.info(f"Successfully discovered {len(endpoints)} endpoints.")
            
            # Save endpoints to a separate file
            endpoints_file = os.path.join(output_dir, f"discovered_endpoints_{timestamp}.json")
            with open(endpoints_file, 'w') as f:
                json.dump(endpoints, f, indent=2)
            logging.info(f"Saved discovered endpoints to: {endpoints_file}")
            
            return endpoints
        else:
            logging.warning("No endpoints discovered or parsing failed.")
            return []

    except subprocess.TimeoutExpired:
        logging.error(f"Endpoint discovery timed out after {timeout} seconds.")
        return []
    except Exception as e:
        logging.error(f"An error occurred during endpoint discovery: {e}")
        return []