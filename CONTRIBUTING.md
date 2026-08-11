# Contributing to Inspectarr

Thanks for your interest in contributing! Inspectarr is a homelab project and contributions of all sizes are welcome.

## Getting Started

1. Fork the repo and clone it locally
2. Copy `config.example.yaml` to `config.yaml` and fill in your connection details
3. Run from source: `pip install -r requirements.txt && python3 web.py`
4. Or use the dev container: open in VS Code with the Dev Containers extension

## Submitting Changes

- Open an issue first for large features so we can discuss the approach
- Keep PRs focused — one feature or fix per PR
- Follow the existing code style (Python, Flask, Jinja2)
- Test your changes against a running qBittorrent + Prowlarr setup if possible

## Bug Reports

Use the [bug report template](https://github.com/o51r15/inspectarr/issues/new?template=bug_report.md). Include your Inspectarr version, Docker or source install, and any relevant log output.

## Feature Requests

Use the [feature request template](https://github.com/o51r15/inspectarr/issues/new?template=feature_request.md). Describe the problem you're trying to solve, not just the solution you want.

## Code of Conduct

Be respectful. This is a hobby project maintained in spare time. Constructive feedback is welcome; demands are not.
