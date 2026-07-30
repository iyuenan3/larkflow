# Repository Guidelines

## Project Structure & Module Organization

`larkflow/` is the Python package. Orchestration lives in `engine/`, workflow contracts in `model/`, Lark adapters in `io/`, and model routing in `llm/`. Business workflows belong in `larkflow/templates/*.yaml`, not new Python executor types. Tests live in `tests/`; deployment assets live in `deploy/`.

Treat `AIREADME/` as the design source of truth. Start with `AIREADME/INDEX.md`, then read the document it routes to before changing architecture, conventions, or deployment.

## Build, Test, and Development Commands

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"                # editable install plus pytest
pytest -q                              # run the complete offline suite
pytest -q tests/test_orchestrator.py   # run one focused module
python -m larkflow.demo --auto         # exercise the contract flow locally
python -m larkflow.demo --template hiring
```

Tests must use mock Lark I/O, stub LLMs, and in-memory SQLite. Never invoke `build_real_service` in tests. `larkflow serve` can create real Lark resources; run it only with an intentional dev configuration.

## Coding Style & Naming Conventions

Target Python 3.10+. Use four-space indentation, `snake_case` functions and modules, `PascalCase` classes, uppercase constants, type hints, and short docstrings. Keep nodes data-driven as `executor × role` dictionaries and use lower-case `snake_case` YAML IDs. No formatter or linter is configured; match nearby code and keep imports clean.

## Testing Guidelines

Pytest discovers `tests/test_*.py`. Name tests after observable behavior, add regression coverage for bug fixes, and reuse `tests/support.py`. Run the full suite before submitting; no numeric coverage threshold is configured. Avoid network, credentials, and machine-specific state.

## Commit & Pull Request Guidelines

History uses Conventional Commit-style subjects: `feat(engine): ...`, `fix(llm): ...`, `test: ...`, or `docs(aireadme): ...`. Keep commits focused with specific summaries.

Pull requests should explain the behavior change, affected invariants, and verification commands; link the issue or ADR when applicable. Include screenshots or sanitized event/card payloads for user-visible Lark changes. Update the appropriate `AIREADME` files when architecture, public contracts, deployment, milestones, or operational lessons change.

## Security & Configuration

Copy `.env.example` locally and never commit credentials, tokens, real user IDs, or production database files. Keep SQLite on a local disk. Authorization and graph-edit legality must be calculated by the engine; never trust card action payloads or other client-supplied identity fields.
