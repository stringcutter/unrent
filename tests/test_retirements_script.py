"""scripts/retirements.py: the deprecation-page parser and the file it writes, without network."""

from __future__ import annotations

import datetime
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent

# The shapes the three pages use: OpenAI's dated announcements (an older one listed first
# here, so the date decides, not the position), Anthropic's status overview and Google's
# per-family tables with bold headers and a "Preview models" divider row.
PAGE = """\
# Deprecations

### 2025-04-14: GPT-4.5 preview

| Shutdown date | Model / system | Recommended replacement |
| ------------- | -------------- | ----------------------- |
| 2026‑07‑14 | `gpt-4.5-preview` | gpt-4.1† |
| 2026‑07‑14 | `ft-gpt-4.5-preview` | `gpt-4.1` |

## 2026-06-11: GPT-5 and o3

| Shutdown date | Model / system | Recommended replacement |
| ------------- | -------------- | ----------------------- |
| Dec 11, 2026 | `gpt-4.5-preview` | `gpt-5.6-sol` (`reasoning.mode: pro`) |
| Dec 11, 2026 | `o3-2025-04-16` | `gpt-5.6-sol` |
| Oct 1, 2026 | `gpt-5.4-cyber` | The most capable cyber model available to you. |
| Dec 11, 2026 | `/v1/edits` | `/v1/chat/completions` |
| Dec 11, 2026 | Assistants API | Responses API |
| August 2026 | `o1-mini` | `o3` |

## Model status

| API model name | Current state | Deprecated | Tentative retirement date |
| :--- | :--- | :--- | :--- |
| claude-3-haiku-20240307 | Retired | February 19, 2026 | April 20, 2026 |

## Gemini 2.5 Flash models

| **Model** | **Release date** | **Shutdown date** | **Recommended replacement** |
|---|---|---|---|
| `gemini-2.5-flash` | June 17, 2025 | No shutdown date announced |   |
| Preview models ||||
| `gemini-2.5-flash-image` | October 2, 2025 | October 2, 2026 | `gemini-3.1-flash-image` |
"""


@pytest.fixture(scope="module")
def retirements():
    spec = importlib.util.spec_from_file_location(
        "retirements", ROOT / "scripts" / "retirements.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_reads_every_page_shape(retirements):
    models, notes = retirements.parse(PAGE)
    d = datetime.date
    assert models == {
        "gpt-4.5-preview": {"retires": d(2026, 12, 11), "replacement": "gpt-5.6-sol"},
        "o3-2025-04-16": {"retires": d(2026, 12, 11), "replacement": "gpt-5.6-sol"},
        "gpt-5.4-cyber": {"retires": d(2026, 10, 1), "replacement": None},
        "gemini-2.5-flash-image": {
            "retires": d(2026, 10, 2),
            "replacement": "gemini-3.1-flash-image",
        },
    }
    assert notes == [
        "skipped ft-gpt-4.5-preview: not a model id",
        "conflict gpt-4.5-preview: kept 2026-12-11 -> gpt-5.6-sol, dropped 2026-07-14 -> gpt-4.1",
        "skipped /v1/edits: not a model id",
        "skipped Assistants API: not a model id",
        "skipped o1-mini: no exact date ('August 2026')",
    ]


def test_render_round_trips(retirements):
    models, _ = retirements.parse(PAGE)
    # Ids and replacements YAML would read as a float, a bool or null stay strings.
    models["4.5"] = {"retires": datetime.date(2027, 1, 6), "replacement": "null"}
    models["yes"] = {"retires": datetime.date(2027, 1, 6), "replacement": "1e3"}
    vendors = {
        "openai": {
            "url": "https://developers.openai.com/api/docs/deprecations",
            "services": ["openai", "azure-openai"],
            "models": models,
        },
        "google": {
            "url": "https://ai.google.dev/gemini-api/docs/deprecations",
            "services": ["gemini"],
            "models": {
                "gemini-2.0-flash": {"retires": datetime.date(2026, 6, 1), "replacement": None}
            },
        },
    }
    text = retirements.render("# Header comment.\n", vendors)
    assert text.startswith("# Header comment.\nvendors:\n")
    assert yaml.safe_load(text) == {"vendors": vendors}
