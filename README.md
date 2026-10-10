# Polymarket BTC 15-minute Paper-Trading Bot

A Python project for a future BTC 15-minute Up/Down paper-trading bot.
Paper trading means simulating trades without spending real money.

## Current scope

This is project scaffolding only. There is no trading strategy, market data,
Polymarket connection, wallet, credential handling, or order placement.
The starter program only prints a local setup message.

## Requirements

- Python 3.12 or newer
- uv for virtual environments and dependency management
- Git for tracking changes

There are currently no third-party dependencies. Python's built-in tools are
sufficient for this setup.

## Setup and run

Run these commands in PowerShell from the project folder:

```powershell
uv sync
uv run python src/main.py
```

uv creates and manages `.venv`, a separate Python environment for this project.
You do not need to activate it manually when using `uv run`.

If Python is not on PATH, give uv the full path to an installed Python executable:

```powershell
uv sync --python "C:\path\to\python.exe" --no-python-downloads
uv run python src/main.py
```

The initial setup used the Python 3.12 interpreter bundled with Codex.
To verify the environment without accessing the network:

```powershell
uv run --offline --locked python --version
uv run --offline --locked python src/main.py
```

Expected program output:

```text
Project setup OK: Polymarket BTC 15-minute paper-trading bot.
Scaffolding only. No network connections or orders.
```

## Project layout

```text
Polymarket-bot/
  .env.example       Template for future local settings
  .gitignore         Files Git should leave out
  .python-version    Default Python version for uv
  pyproject.toml     Project information and dependency settings
  uv.lock            Dependency lock file managed by uv
  README.md          Project guide
  src/
    main.py          Local startup sanity check
  tests/
    .gitkeep         Keeps the empty test folder in Git
  data/
    .gitkeep         Keeps the empty data folder in Git
  logs/
    .gitkeep         Keeps the empty log folder in Git
```

`tests/` is reserved for tests as features are added. `data/` and `logs/`
are reserved for future local output; generated contents are ignored by Git.
`.venv/` and `.uv-cache/` are also ignored.

`.env.example` is only a placeholder. The program does not read environment
files yet. Do not add private keys, wallet details, or account credentials.

## Dependencies and version control

Use `uv add package-name` when a future step needs a runtime dependency, or
`uv add --dev package-name` for a development tool. uv updates `pyproject.toml`
and `uv.lock`; keep both files in Git.

Git has been initialized locally. No commit or remote repository has been
created. Use `git status` to see the starter files.
