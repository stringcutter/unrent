#!/usr/bin/env python3
"""Check catalog/retirements.yaml against the vendors' own deprecation pages.

    python scripts/retirements.py [--check | --write] [--notes] [--file PATH]

Each vendor publishes its model retirements as Markdown tables (the `.md` form of the
docs page), one table per announcement, newest first: a shutdown date, the model ids
and the recommended replacement. Rows that name an endpoint, a parameter or a product
("Assistants API") are skipped, and so are fine-tuned ids (`ft-...`) and the
`-completions` pseudo-ids, which a client cannot send as a model.

--check   (default) Print what differs from the file: new ids, changed dates or
          replacements, ids the page no longer lists. Exit 1 if anything differs.
--write   Rewrite the file from the pages, keeping its header comment and each
          vendor's `url` and `services`, for a weekly pull request. Exits 0.
--notes   Also print the rows that were skipped and the ids listed twice with
          different dates (the most recent announcement wins).

Exits 2 when a page cannot be fetched or yields no rows: a parser that finds nothing
has broken, it has not found "no changes".
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import time
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "catalog" / "retirements.yaml"
UA = "unrent-retirements (+https://github.com/stringcutter/unrent)"
# The Markdown form of each page; the `url` in the file is the page people read.
SOURCES = {
    "openai": "https://developers.openai.com/api/docs/deprecations.md",
    "anthropic": "https://platform.claude.com/docs/en/about-claude/model-deprecations.md",
    "google": "https://ai.google.dev/gemini-api/docs/deprecations.md.txt",
}
MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$", re.I)
CODE = re.compile(r"`([^`]+)`")
DATED_HEADING = re.compile(r"^#{2,4}\s+(\d{4}-\d{2}-\d{2})")


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/markdown"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8")


def parse_date(text: str) -> dt.date | None:
    # OpenAI writes some dates with non-breaking hyphens (U+2011).
    text = text.replace("\u2011", "-").replace("\xa0", " ").strip()
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
        try:
            return dt.date(*time.strptime(text, fmt)[:3])
        except ValueError:
            pass
    return None  # "No shutdown date announced", "at earliest 2024-06-13", "August 2026"


def _cells(line: str) -> list[str]:
    parts = re.split(r"(?<!\\)\|", line.strip().strip("|"))
    return [p.replace("\\|", "|").strip() for p in parts]


def tables(text: str):
    """(announcement date or None, header cells, rows) for every Markdown table, with
    the date of the nearest heading above that starts with one."""
    lines = text.splitlines()
    announced = None
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#"):
            m = DATED_HEADING.match(line)
            if m:
                announced = dt.date.fromisoformat(m.group(1))
            elif line.startswith(("# ", "## ")):
                announced = None
        if (
            line.startswith("|")
            and i + 1 < len(lines)
            and re.fullmatch(r"\|?[\s:|-]+\|?", lines[i + 1].strip())
        ):
            header = [h.strip("*").lower() for h in _cells(line)]
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            yield announced, header, rows
            continue
        i += 1


def _column(header: list[str], *words: str, avoid: tuple[str, ...] = ()) -> int | None:
    for n, h in enumerate(header):
        if any(w in h for w in words) and not any(a in h for a in avoid):
            return n
    return None


def model_ids(cell: str) -> tuple[list[str], list[str]]:
    """The model ids a row names, and the ones it names that are not models."""
    cell = cell.strip()
    if not cell.startswith("`"):
        # "Videos API", "New fine-tuning training on `babbage-002`"; a bare id is a model.
        return ([cell], []) if MODEL_ID.match(cell) else ([], [cell] if cell else [])
    ids, skipped = [], []
    for raw in CODE.findall(cell):
        token = raw.strip()
        if token.startswith("/") or token.startswith("ft-") or token.endswith("-completions"):
            skipped.append(token)
        elif MODEL_ID.match(token):
            ids.append(token)
        else:
            skipped.append(token)
    return ids, skipped


def replacement(cell: str) -> str | None:
    """The first model id the vendor names, without footnote marks; None for "---" or
    advice that is not a model ("The most capable cyber model available to you.")."""
    named = CODE.findall(cell)
    first = named[0].strip() if named else cell.strip()
    first = first.rstrip("*†‡")
    return first if MODEL_ID.match(first) else None


def parse(text: str) -> tuple[dict[str, dict], list[str]]:
    """{model id: {retires, replacement}} and notes on rows that were skipped or
    conflicted. A model listed in two announcements keeps the most recent one; the
    page lists announcements newest first, so a tie keeps the first row."""
    found: dict[str, dict] = {}
    notes: list[str] = []
    for order, (announced, header, rows) in enumerate(tables(text)):
        when = _column(header, "shutdown date", "retirement date")
        if when is None or "tentative retirement date" in header:
            continue  # "Date | Update" tables and Anthropic's model status overview
        model = _column(
            header, "model", "system", "agent", avoid=("price", "replacement", "substitute")
        )
        repl = _column(header, "replacement", "substitute")
        if model is None or repl is None or "agent" in header[model]:
            continue  # Google's managed agents are not model ids
        for row in rows:
            if len(row) <= max(when, model, repl) or not row[model]:
                continue  # "Preview models ||||"
            ids, skipped = model_ids(row[model])
            if skipped:
                notes.append(f"skipped {', '.join(skipped)}: not a model id")
            if not ids:
                continue
            date = parse_date(row[when])
            if date is None:
                if "no shutdown date" not in row[when].lower():
                    notes.append(f"skipped {', '.join(ids)}: no exact date ({row[when]!r})")
                continue
            entry = {"retires": date, "replacement": replacement(row[repl])}
            rank = (announced or dt.date.min, -order)
            for mid in ids:
                old = found.get(mid)
                if old and (old["retires"], old["replacement"]) != (date, entry["replacement"]):
                    keep, drop = (old, entry) if old["_rank"] >= rank else (entry, old)
                    notes.append(
                        f"conflict {mid}: kept {keep['retires']} -> {keep['replacement']}, "
                        f"dropped {drop['retires']} -> {drop['replacement']}"
                    )
                if not old or rank > old["_rank"]:
                    found[mid] = {**entry, "_rank": rank}
    models = {
        k: {"retires": v["retires"], "replacement": v["replacement"]} for k, v in found.items()
    }
    return models, list(dict.fromkeys(notes))


def ordered(models: dict[str, dict]) -> list[tuple[str, dict]]:
    """Newest retirement first, then id."""
    by_id = sorted(models.items())
    return sorted(by_id, key=lambda kv: kv[1]["retires"], reverse=True)


def _scalar(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, dt.date):
        return value.isoformat()
    plain = yaml.safe_load(f"k: {value}") == {"k": value} and not re.search(r"[{}\[\],#]", value)
    return value if plain else yaml.safe_dump(value, default_style='"').strip()


def render(header: str, vendors: dict[str, dict]) -> str:
    out = [header.rstrip("\n"), "vendors:"]
    for n, (name, v) in enumerate(vendors.items()):
        out += [f"  {name}:", f"    url: {v['url']}"]
        if n == 0:
            out.append("    # The catalog services whose model ids these are.")
        out += [f"    services: [{', '.join(v['services'])}]", "    models:"]
        for mid, m in ordered(v["models"]):
            out.append(
                f"      {_scalar(mid)}: {{retires: {_scalar(m['retires'])}, "
                f"replacement: {_scalar(m['replacement'])}}}"
            )
    return "\n".join(out) + "\n"


def diff(old: dict[str, dict], new: dict[str, dict]) -> list[str]:
    lines = []
    for mid in sorted(new.keys() - old.keys()):
        lines.append(f"  new      {mid}: {new[mid]['retires']} -> {new[mid]['replacement']}")
    for mid in sorted(old.keys() & new.keys()):
        a, b = old[mid], new[mid]
        if a["retires"] != b["retires"]:
            lines.append(f"  date     {mid}: {a['retires']} -> {b['retires']}")
        if a["replacement"] != b["replacement"]:
            lines.append(f"  replaced {mid}: {a['replacement']} -> {b['replacement']}")
    for mid in sorted(old.keys() - new.keys()):
        lines.append(f"  gone     {mid}: no longer on the page")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report differences (default)")
    mode.add_argument("--write", action="store_true", help="rewrite the file from the pages")
    ap.add_argument("--file", type=Path, default=FILE)
    ap.add_argument("--notes", action="store_true", help="also print skipped rows and conflicts")
    a = ap.parse_args()
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    text = a.file.read_text(encoding="utf-8")
    top = text.splitlines()
    header = "\n".join(top[: next(n for n, ln in enumerate(top) if not ln.startswith("#"))])
    current = yaml.safe_load(text)["vendors"]

    pages: dict[str, dict] = {}
    for name in current:
        if name not in SOURCES:
            print(f"{name}: no source page known to this script", file=sys.stderr)
            return 2
        try:
            models, notes = parse(fetch(SOURCES[name]))
        except Exception as e:
            print(f"{name}: could not read {SOURCES[name]}: {e}", file=sys.stderr)
            return 2
        if not models:
            print(f"{name}: no retirement rows found in {SOURCES[name]}", file=sys.stderr)
            return 2
        pages[name] = models
        if a.notes:
            for note in notes:
                print(f"{name}: {note}", file=sys.stderr)

    changed = 0
    for name, v in current.items():
        old = v.get("models") or {}
        lines = diff(old, pages[name])
        print(f"{name}: {len(pages[name])} models on the page, {len(lines)} differences")
        if lines:
            print("\n".join(lines))
        changed += len(lines)

    if a.write:
        vendors = {name: {**v, "models": pages[name]} for name, v in current.items()}
        a.file.write_text(render(header, vendors), encoding="utf-8")
        print(f"Wrote {a.file}", file=sys.stderr)  # not into the pull request body
        return 0
    return 1 if changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
