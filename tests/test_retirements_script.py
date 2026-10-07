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


# The platforms: Azure lists a model once per version (and tuned models apart), Bedrock
# names provider and model before the id and lists the models past their end of life as
# bullets, Vertex links its ids and gives partner models' dates in prose.
PLATFORMS = """\
### Azure OpenAI

| Model | Version | Lifecycle | Retirement date | Replacement |
|-------|---------|-----------|-----------------|-------------|
| gpt-4o | 2024-05-13 | Deprecated | 2026-12-09 | gpt-5.6-sol |
| gpt-4o | 2024-08-06 | Deprecated | 2027-04-14 | gpt-5.1 |
| gpt-realtime-2 | 2026-05-06 | Preview | — | gpt-realtime-2.1 |
| gpt-realtime-2 | 2026-07-01 | Preview | 2026-11-01 | — |
| Cohere-rerank-v3.5 | 1 | Retired | 2026-05-14 | Cohere-rerank-v4.0-pro, Cohere-rerank-v4.0-fast |

| Model | Version | Training retirement date | Deployment retirement date |
|-------|---------|--------------------------|----------------------------|
| gpt-4o | 2024-08-06 | 2027-04-01 | 2027-10-01 |

| Model provider | Model name | Model ID | Regions | Legacy date | EOL date | Public extended access start date |
| --- | --- | --- | --- | --- | --- | --- |
| Anthropic | Claude Sonnet 4 | anthropic.claude-sonnet-4-20250514-v1:0 | us-east-1 | April 14, 2026 | October 14, 2026 | July 14, 2026 |

- **Anthropic**
  - **Model name:** Claude 3 Haiku
  - **Model ID:** anthropic.claude-3-haiku-20240307-v1:0
  - **Regions:** us-east-1 / **Legacy date:** March 10, 2026 / **EOL date:** September 10, 2026
  - **Regions:** us-gov-east-1 / **Legacy date:** March 10, 2026 / **EOL date:** October 1, 2026

### Retired models

| Model ID | Release date | Retirement date | Recommended upgrade |
|---|---|---|---|
| [`gemini-2.5-pro`](https://example.org/2-5-pro) | June 17, 2025 | October 20, 2026 | [`gemini-3.8-flash`](https://example.org/3-8) or [`gemini-3.5-flash`](https://example.org/3-5) |
| `textembedding-gecko@003\\*` | December 12, 2023 | May 24, 2025 | `gemini-embedding-001` |

## Claude 3.5 Sonnet v2 on Google Cloud

Claude 3.5 Sonnet v2 on Google Cloud is **deprecated as of August 20, 2025** and
will be
**shut down on February 19, 2026**.

| Model ID | `claude-3-5-sonnet-v2` ||
| Launch stage | GA ||
"""


def test_a_dated_block_without_one_model_id_is_noted(retirements):
    page = "## Two models\n\nThey **shut down on May 1, 2026**.\n\n| Model ID | `a` ||\n| Model ID | `b` ||\n"
    assert retirements.parse(page) == (
        {},
        ["skipped '## Two models': dates for 2 model ids, not one"],
    )


def test_parse_reads_the_platform_pages(retirements):
    models, _ = retirements.parse(PLATFORMS)
    d = datetime.date
    assert models == {
        # A name works until its last version retires, and on while one has no date.
        "gpt-4o": {"retires": d(2027, 4, 14), "replacement": "gpt-5.1"},
        "Cohere-rerank-v3.5": {"retires": d(2026, 5, 14), "replacement": "Cohere-rerank-v4.0-pro"},
        "anthropic.claude-sonnet-4-20250514-v1:0": {"retires": d(2026, 10, 14), "replacement": None},
        # Out of service in the last of its Regions.
        "anthropic.claude-3-haiku-20240307-v1:0": {"retires": d(2026, 10, 1), "replacement": None},
        "gemini-2.5-pro": {"retires": d(2026, 10, 20), "replacement": "gemini-3.8-flash"},
        "textembedding-gecko@003": {"retires": d(2025, 5, 24), "replacement": "gemini-embedding-001"},
        "claude-3-5-sonnet-v2": {"retires": d(2026, 2, 19), "replacement": None},
    }  # fmt: skip
    # Under a key of their own, which releases up to 0.3.0 do not read.
    vendors = {
        "bedrock": {"url": "https://example.org", "services": ["aws-bedrock"], "models": models},
        "openai": {
            "url": "https://example.org",
            "services": ["openai"],
            "models": {"o1": models["gpt-4o"]},
        },
    }
    text = retirements.render("# Header comment.\n", vendors, {"bedrock"})
    assert yaml.safe_load(text) == {
        "vendors": {"openai": vendors["openai"]},
        "platforms": {"bedrock": vendors["bedrock"]},
    }
