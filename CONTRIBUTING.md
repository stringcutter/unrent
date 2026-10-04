# Contributing

Thanks for helping. Most contributions are catalog changes, and those are plain YAML.

## Report a false positive or a missed service

Open an issue with the template. The most useful thing you can include is the exact
line that fooled unrent (or that it missed) and the file name, since detection
depends on both. Mask any real keys; unrent masks them in its own report.

## Add a closed service

Add an entry to the right file in [`catalog/services/`](catalog/services): its
`id`, `name`, `category`, the pools in `replace_with`, and `detect` signatures
(packages, imports, hosts, model ids, environment variables). Not to
`models-dev.yaml`: that file is generated from [models.dev](https://models.dev) every
week by `scripts/new_services.py`, and a hand-written entry sharing a host, key or
package replaces its generated one at the next refresh. To see what is missing:

```bash
uv run python scripts/new_services.py --models-dev   # hosted providers on models.dev
uv run unrent scan path/to/project                     # "Possibly closed AI services ..."
```

Then:

```bash
uv run unrent catalog --validate
uv run pytest -q
```

Add a test to `tests/test_unrent.py` showing a snippet that must be found, and one
that looks similar but must not be.

## Suggest an open source alternative

Add it to a pool in [`catalog/alternatives.yaml`](catalog/alternatives.yaml) with
`repo: owner/name` and a one-line `what`. It must have an OSI-approved licence, not
be archived, and have had a push in the last year. You do not need to decide its
position: the weekly refresh ranks it.

## Code changes

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```

If [ripgrep](https://github.com/BurntSushi/ripgrep) is installed, the detection tests
run twice, with and without it, and must agree. CI does this on Linux.

## Releasing (maintainers)

1. Update `version` in `pyproject.toml` and move `Unreleased` in `CHANGELOG.md`.
2. Merge the latest "Refresh rankings" pull request so the release ships current data.
3. Tag `vX.Y.Z` and publish a GitHub release; `.github/workflows/publish.yml` uploads
   to PyPI through trusted publishing.
