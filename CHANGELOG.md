# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Weekly ranking refreshes are not listed;
each release ships the rankings as of its date.

## [Unreleased]

## [0.2.0] - 2026-09-26

First public release.

### Added
- `unrent scan <dir>`: finds closed AI services (about 100: LLM APIs, gateways,
  embeddings, vector databases, RAG, OCR, observability, speech, image generation,
  search, browser automation, sandboxes, agent memory) from packages, imports,
  install commands, framework integrations, API hosts, model ids, environment
  variables and Terraform resources.
- Markdown and JSON reports, with file, line and masked text for every finding.
- Open source alternatives per service, ranked weekly by GitHub star momentum and
  Hugging Face trending (`scripts/refresh.py`).
- `--exclude`, `.unrentignore`, `--skip-tests`, `--top`, `--output`.
- `unrent catalog` and `unrent catalog --validate`.
- Optional ripgrep acceleration with identical results.

[Unreleased]: https://github.com/niklasmellgren/unrent/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/niklasmellgren/unrent/releases/tag/v0.2.0
