# Contributing to anime-pahe-dl

First off, thank you for considering contributing to `anime-pahe-dl`! It's people like you that make open source such a
great community.

## Development Setup

1. **Fork and clone the repository:**
   ```bash
   git clone https://github.com/YOUR_USERNAME/anime-pahe-dl.git
   cd anime-pahe-dl
   ```

2. **Set up a virtual environment (recommended):**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install the package with development dependencies:**
   ```bash
   pip install -e ".[dev,test]"
   ```

4. **Install pre-commit hooks:**
   We use `pre-commit` to ensure code formatting and linting rules are consistently applied.
   ```bash
   pre-commit install
   ```

## Workflow

1. Create a new branch for your feature or bug fix:
   ```bash
   git checkout -b feature/my-new-feature
   ```
2. Make your changes.
3. Commit your changes. The pre-commit hooks will automatically format your code using `ruff`.
4. Run tests to ensure nothing is broken:
   ```bash
   pytest tests/ -v
   ```
5. Push to your fork:
   ```bash
   git push origin feature/my-new-feature
   ```
6. Open a Pull Request!

## Code Style

- We use `ruff` for fast linting and code formatting.
- Try to add type hints (`mypy`) for new functions or classes.

## Getting Help

If you have questions about the codebase, feel free to open a "Question" issue or ask in your PR. We're happy to help!
