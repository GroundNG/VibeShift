# Contributing to the AI Web Testing Agent

First off, thank you for considering contributing! This project aims to improve the development workflow by integrating automated web testing directly with AI coding assistants. Your contributions can make a real difference.

## Code of Conduct

This project and everyone participating in it is governed by the [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code. Please report unacceptable behavior. 

## How Can I Contribute?

There are many ways to contribute, from reporting bugs to implementing new features.

### Reporting Bugs

*   Ensure the bug was not already reported by searching on GitHub under [Issues](https://github.com/Ilikepizza2/GroundNG/issues). 
*   If you're unable to find an open issue addressing the problem, [open a new one](https://github.com/Ilikepizza2/GroundNG/issues/new).  Be sure to include a **title and clear description**, as much relevant information as possible, and a **code sample or an executable test case** demonstrating the expected behavior that is not occurring.
*   Include details about your environment (OS, Python version, library versions).

### Suggesting Enhancements

*   Open a new issue to suggest an enhancement. Provide a clear description of the enhancement and its potential benefits.
*   Explain why this enhancement would be useful and provide examples if possible.

### Pull Requests

1.  **Fork the repository** on GitHub.
2.  **Clone your fork** locally: `git clone git@github.com:Ilikepizza2/GroundNG.git`
3.  **Create a virtual environment** and install dependencies:
    ```bash
    cd <repository-name>
    python -m venv venv
    source venv/bin/activate # Or venv\Scripts\activate on Windows
    pip install -r requirements.txt
    playwright install --with-deps
    ```
4.  **Create a topic branch** for your changes: `git checkout -b feature/your-feature-name` or `git checkout -b fix/your-bug-fix`.
5.  **Make your changes.** Write clean, readable code. Add comments where necessary.
6.  **Add tests** for your changes. Ensure existing tests pass. (See Testing section below).
7.  **Format your code** (e.g., using Black): `black .`
8.  **Commit your changes** using a descriptive commit message. Consider using [Conventional Commits](https://www.conventionalcommits.org/).
9.  **Push your branch** to your fork on GitHub: `git push origin feature/your-feature-name`.
10. **Open a Pull Request** to the `main` branch of the original repository. Provide a clear description of your changes and link any relevant issues.

## Development Setup

*   Follow the installation steps in the [README.md](README.md).
*   Ensure you have a `.env` file set up with your LLM API key for running the agent components that require it.
*   Use the `mcp dev mcp_server.py` command to run the server locally for testing MCP interactions.

## Testing

*   This project uses `pytest`. Run tests using:
    ```bash
    pytest
    ```
*   Please add tests for any new features or bug fixes. Place tests in a `tests/` directory (if not already present).
*   Ensure all tests pass before submitting a pull request.

## Code Style

*   Please follow PEP 8 guidelines.
*   We recommend using [Black](https://github.com/psf/black) for code formatting. Run `black .` before committing.
*   Use clear and descriptive variable and function names.
*   Add docstrings to modules, classes, and functions.

## Questions?

If you have questions about contributing or the project in general, feel free to open an issue on GitHub.

Thank you for your contribution!