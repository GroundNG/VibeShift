# VibeShift: The Security Engineer for Vibe Coders

**VibeShift** is an intelligent security agent designed to integrate seamlessly with AI coding assistants (like Cursor, GitHub Copilot, Claude Code, etc.). It acts as your automated security engineer, analyzing code generated by AI, identifying vulnerabilities, and facilitating AI-driven remediation *before* insecure code makes it to your codebase. It leverages the **MCP (Model Context Protocol)** for smooth interaction within your existing AI coding environment.

<a href="https://www.producthunt.com/posts/vibeshift-mcp?embed=true&utm_source=badge-featured&utm_medium=badge&utm_source=badge-vibeshift&#0045;mcp" target="_blank"><img src="https://api.producthunt.com/widgets/embed-image/v1/featured.svg?post_id=966186&theme=light&t=1747654611925" alt="VibeShift&#0032;MCP - Get&#0032;secure&#0044;&#0032;working&#0032;code&#0032;in&#0032;1&#0032;shot | Product Hunt" style="width: 115px; height: 25px;" width="250" height="54" /></a>
[![Twitter Follow](https://img.shields.io/twitter/follow/Omiiee_Chan?style=social)](https://x.com/Omiiee_Chan)
[![Twitter Follow](https://img.shields.io/twitter/follow/_gauravkabra_?style=social)](https://x.com/_gauravkabra_)
![](https://img.shields.io/github/stars/groundng/vibeshift)


**The Problem:** AI coding assistants accelerate development dramatically, but they can also generate code with subtle or overt security vulnerabilities. Manually reviewing all AI-generated code for security flaws is slow, error-prone, and doesn't scale with the speed of AI development. This "vibe-driven development" can leave applications exposed.

**The Solution: GroundNG's VibeShift** bridges this critical security gap by enabling your AI coding assistant to:

1.  **Automatically Analyze AI-Generated Code:** As code is generated or modified by an AI assistant, VibeShift can be triggered to perform security analysis using a suite of tools (SAST, DAST components) and AI-driven checks.
2.  **Identify Security Vulnerabilities:** Pinpoints common and complex vulnerabilities (e.g., XSS, SQLi, insecure configurations, logic flaws) within the AI-generated snippets or larger code blocks.
3.  **Facilitate AI-Driven Remediation:** Provides detailed feedback and vulnerability information directly to the AI coding assistant, enabling it to suggest or even automatically apply fixes.
4.  **Create a Security Feedback Loop:** Ensures that developers and their AI assistants are immediately aware of potential security risks, allowing for rapid correction and learning.

This creates a "shift-left" security paradigm for AI-assisted coding, embedding security directly into the development workflow and helping to ship more secure code, faster.

# Demo (Click to play these videos)
[![Demo](https://img.youtube.com/vi/bN_RgQGa8B0/maxresdefault.jpg)](https://www.youtube.com/watch?v=bN_RgQGa8B0)
[![Click to play](https://img.youtube.com/vi/wCbCUCqjnXQ/maxresdefault.jpg)](https://youtu.be/wCbCUCqjnXQ)


## Features

*   **MCP Integration:** Seamlessly integrates with Cursor/Windsurf/Github Copilot/Roo Code
*   **Automated Security Scanning:** Triggers on AI code generation/modification to perform:
    *   **Static Code Analysis (SAST):** Integrates tools like Semgrep to find vulnerabilities in source code.
    *   **Dynamic Analysis (DAST Primitives):** Can invoke tools like Nuclei or ZAP for checks against running components (where applicable).
*   **AI-Assisted Test Recording:** Generate Playwright-based test scripts from natural language descriptions (in automated mode).
*   **Deterministic Test Execution:** Run recorded JSON test files reliably using Playwright.
*   **AI-Powered Test Discovery:** Crawl websites and leverage any LLM (in openai compliant format) to suggest test steps for discovered pages.
*   **Regression Testing:** Easily run existing test suites to catch regressions.
*   **Automated Feedback Loop:** Execution results (including failures, screenshots, console logs) are returned, providing direct feedback to the AI assistant.
*   **Self Healing:** Existing tests self heal in case of code changes. No need to manually update.
*   **UI tests:** UI tests which aren't supported by playwright directly are also supported. For example, `Check if the text is overflowing in the div`
*   **Visual Regression Testing**: Using traditional pixelmatch and vision LLM approach.

## How it Works

```
+-------------+       +-----------------+       +---------------------+       +-----------------+       +-------------+
|    User     | ----> | AI Coding Agent | ----> |     MCP Server      | ----> | Scan, test, exec| ----> | Browser     |
| (Developer) |       | (e.g., Copilot) |       | (mcp_server.py)     |       | (SAST, Record)  |       | (Playwright)|
+-------------+       +-----------------+       +---------------------+       +-----------------+       +-------------+
      ^                                                  |                            |                     |
      |--------------------------------------------------+----------------------------+---------------------+
                                      [Test Results / Feedback]
```

1.  **User:** Prompts their AI coding assistant (e.g., "Test this repository for security vulnerabilities", "Record a test for the login flow", "Run the regression test 'test_login.json'").
2.  **AI Coding Agent:** Recognizes the intent and uses MCP to call the appropriate tool provided by the `MCP Server`.
3.  **MCP Server:** Routes the request to the corresponding function (`get_security_scan`, `record_test_flow`, `run_regression_test`, `discover_test_flows`, `list_recorded_tests`).
4.  **VibeShift Agent:**
    *   **Traditional Security Scan:**  Invokes **Static Analysis Tools** (e.g., Semgrep) on the code.
    *   **Recording:** The `WebAgent` (in automated mode) interacts with the LLM to plan steps, controls the browser via `BrowserController` (Playwright), processes HTML/Vision, and saves the resulting test steps to a JSON file in the `output/` directory.
    *   **Execution:** The `TestExecutor` loads the specified JSON test file, uses `BrowserController` to interact with the browser according to the recorded steps, and captures results, screenshots, and console logs.
    *   **Discovery:** The `CrawlerAgent` uses `BrowserController` and `LLMClient` to crawl pages and suggest test steps.
6.  **Browser:** Playwright drives the actual browser interaction.
6.  **Feedback Loop:**
    *   The comprehensive security report (vulnerabilities, locations, suggestions) is returned through the MCP server to the **AI Coding Agent**.
    *   The AI Coding Agent presents this to the developer and can use the information to **suggest or apply fixes**.
    *   The goal is a rapid cycle of code generation -> security scan -> AI-driven fix -> re-scan (optional).

## Getting Started

### Prerequisites

*   Python 3.10+
*   Access to any LLM (gemini 2.0 flash works best for free in my testing)
*   MCP installed (`pip install mcp[cli]`)
*   Playwright browsers installed (`patchright install`)

### Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/GroundNG/VibeShift
    cd VibeShift
    ```
2.  **Create a virtual environment (recommended):**
    ```bash
    python -m venv venv
    source venv/bin/activate # Linux/macOS
    # venv\Scripts\activate # Windows
    ```
3.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
4.  **Install Playwright browsers:**
    ```bash
    patchright install --with-deps # Installs browsers and OS dependencies
    ```

### Configuration

1.  Rename the .env.example to .env file in the project root directory.
2.  Add your LLM API key and other necessary details:
    ```dotenv
    # .env
    LLM_API_KEY="YOUR_LLM_API_KEY"
    ```
    *   Replace `YOUR_LLM_API_KEY` with your actual key.

### Adding the MCP Server
Add this to you mcp config:
```json
{
  "mcpServers": {
    "VibeShift":{
      "command": "uv",
      "args": ["--directory","path/to/cloned_repo", "run", "mcp_server.py"]
    }
  }
}
```


Keep this server running while you interact with your AI coding assistant.

## Usage

Interact with the agent through your MCP-enabled AI coding assistant using natural language.

**Examples:**
*   **Security Analysis:**
     *   **Automatic (Preferred):** VibeShift automatically analyzes code snippets generated or significantly modified by the AI assistant. 
     *   **Explicit Commands:**
          > "VibeShift, analyze this function for security vulnerabilities."
          > "Ask VibeShift to check the Python code Copilot just wrote for SQL injection."
          > "Secure the generated code with VibeShift before committing."

*   **Record a Test:**
    > "Record a test: go to https://practicetestautomation.com/practice-test-login/, type 'student' into the username field, type 'Password123' into the password field, click the submit button, and verify the text 'Congratulations student' is visible."
    *   *(The agent will perform these actions automatically and save a `test_....json` file in `output/`)*

*   **Execute a Test:**
    > "Run the regression test `output/test_practice_test_login_20231105_103000.json`"
    *   *(The agent will execute the steps in the specified file and report PASS/FAIL status with errors and details.)*

*   **Discover Test Steps:**
    > "Discover potential test steps starting from https://practicetestautomation.com/practice/"
    *   *(The agent will crawl the site, analyze pages, and return suggested test steps for each.)*

*   **List Recorded Tests:**
    > "List the available recorded web tests."
    *   *(The agent will return a list of `.json` files found in the `output/` directory.)*

**Output:**
* **Security Reports:** Returned to the AI coding assistant, detailing:
    *   Vulnerability type (e.g., CWE, OWASP category)
    *   Location in code
    *   Severity
    *   Evidence / Explanation
    *   Suggested remediations (often for the AI to action)
*   **Recorded Tests:** Saved as JSON files in the `output/` directory (see `test_schema.md` for format).
*   **Execution Results:** Returned as a JSON object summarizing the run (status, errors, evidence paths). Full results are also saved to `output/execution_result_....json`.
*   **Discovery Results:** Returned as a JSON object with discovered URLs and suggested steps. Full results saved to `output/discovery_results_....json`.


## Inspiration
* **[Browser Use](https://github.com/browser-use/browser-use/)**: The dom context tree generation is heavily inspired from them and is modified to accomodate  static/dynamic/visual elements. Special thanks to them for their contribution to open source.
* **[Semgrep](https://github.com/returntocorp/semgrep)**: A powerful open-source static analysis tool we leverage.
* **[Nuclei](https://github.com/projectdiscovery/nuclei)**: For template-based dynamic scanning capabilities.

  
## Contributing

We welcome contributions! Please see `CONTRIBUTING.md` for details on how to get started, report issues, and submit pull requests. We're particularly interested in:

*   New security analyzer integrations.

## License

This project is licensed under the [APACHE-2.0](LICENSE).


