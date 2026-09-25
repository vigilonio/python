# Contributing

This repository holds the `vigilon` Python package, managed with [uv](https://docs.astral.sh/uv/). The source lives in `src/vigilon/` and the tests in `tests/`.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy
```

## Releasing

Releases go to [PyPI](https://pypi.org/project/vigilon/) from GitHub Actions via Trusted Publishing — no API tokens.

- **Main releases**: every push to `main` runs `release-main.yml`, which bumps the patch version in lockstep (`pyproject.toml` + `uv.lock`), runs the tests, builds the wheel, smoke-tests it in a clean venv against a fake collector (`scripts/smoke_test.py`), publishes it to PyPI, then pushes the `chore(release): X.Y.Z` commit and the `vX.Y.Z` tag. A hand-set, not-yet-tagged version releases as-is instead of being bumped. Reruns are idempotent: a version counts as released once its tag exists, and files already on PyPI are skipped.
- **PR betas**: `pr-beta.yml` builds every PR at a unique PEP 440 pre-release (`X.Y.(Z+1)b<PR>.dev<run>`), runs tests + smoke, and attaches the wheel to the workflow run. Betas never go to PyPI; to try a PR's code, install its git ref: `pip install "vigilon @ git+https://github.com/vigilonio/python@<sha>"`.
- **`vigilon-sdk`** is a reserved, unused name on PyPI (`scripts/pypi-stub/vigilon-sdk`) that points users to `vigilon`.
