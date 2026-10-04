#!/usr/bin/env python3
"""Closed AI services that are not in unrent's catalog yet, from two sources.

    python scripts/new_services.py [--models-dev] [--reports DIR ...] [--drafts FILE]

--models-dev   Compare the catalog with models.dev (https://models.dev/api.json), an open
               registry of hosted model providers with their API URL, key names and npm
               package. A provider none of whose host, key or package the catalog knows
               is missing from it.
--reports DIR  Read unrent JSON reports (the eval writes them to eval/.cache/_reports)
               and rank the `unknown_candidates` of every scanned repo by how many repos
               name them. A host that shows up in several projects is worth a catalog
               entry, whichever registry it is in.
--drafts FILE  Also write catalog entries for the models.dev providers, in the
               `new_services:` shape that scripts/verify_packages.py --extra checks.
               Drafts, not entries: a person reviews the id, category and signatures.

Prints a Markdown summary (for a job summary or an issue). Always exits 0 when the
sources could be read: new services are news for a person, not a failure.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import urllib.request
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from unrent.catalog import load_catalog  # noqa: E402
from unrent.discover import GENERIC_KEY, registered_domain  # noqa: E402

MODELS_DEV = "https://models.dev/api.json"
GENERATED = ROOT / "catalog" / "services" / "models-dev.yaml"
UA = "unrent-new-services (+https://github.com/stringcutter/unrent)"
# Packages shared by many providers: they name a protocol, not a vendor.
SHARED_NPM = {"@ai-sdk/openai-compatible", "@ai-sdk/openai", "@ai-sdk/anthropic", "@ai-sdk/gateway"}
LOCAL = re.compile(r"localhost|127\.0\.0\.1|0\.0\.0\.0|\{|\$")
# Runtimes on your own machine that models.dev lists without an API URL.
LOCAL_RUNTIMES = {"qvac", "lmstudio", "ollama", "llama.cpp", "llamacpp", "jan", "localai"}


def _known(catalog) -> tuple[set[str], set[str], set[str], set[str]]:
    hosts, domains, env, npm = set(), set(), set(), set()
    for s in catalog.detectable:
        for needle in s.detect.get("endpoint", ()):
            host = needle.lower().split("/")[0]
            hosts.add(host)
            domains.add(registered_domain(host))
        env.update(s.detect.get("env", ()))
        npm.update(s.detect.get("npm", ()))
    return hosts, domains, env, npm


def _host(url: str | None) -> str | None:
    m = re.match(r"https?://([^/:]+)", url or "")
    return m.group(1).lower() if m else None


def models_dev_missing(catalog) -> list[dict]:
    req = urllib.request.Request(MODELS_DEV, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    hosts, domains, env, npm = _known(catalog)
    missing = []
    for pid, p in sorted(data.items()):
        api = p.get("api")
        host = _host(api)
        if (api and LOCAL.search(api)) or pid in LOCAL_RUNTIMES:
            continue  # a server you run (LM Studio, Ollama): not a hosted service
        package = p.get("npm") if p.get("npm") not in SHARED_NPM else None
        keys = [k for k in p.get("env") or [] if k]
        if (
            (host and (host in hosts or registered_domain(host) in domains))
            or any(k in env for k in keys)
            or (package and package in npm)
        ):
            continue
        if not host and not keys and not package:
            continue  # nothing a scanner could match
        missing.append(
            {
                "id": pid,
                "name": p.get("name") or pid,
                "host": host,
                "api": api,
                "env": keys,
                "npm": package,
                # reached through the openai package with another base URL
                "compatible": p.get("npm") in (None, "@ai-sdk/openai-compatible", "@ai-sdk/openai"),
                "doc": p.get("doc"),
                "models": len(p.get("models") or {}),
            }
        )
    return missing


def _hand_catalog(path: Path):
    """The catalog without the generated file: what people curated. Loading the
    generated file too would make its own entries look known (and a hand entry that
    replaces one would clash with it until the next refresh)."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "catalog"
        shutil.copytree(path, copy, ignore=shutil.ignore_patterns(GENERATED.name, "*.json"))
        for name in ("rankings.json",):
            if (path / name).is_file():
                shutil.copy(path / name, copy / name)
        return load_catalog(copy)


def _vendor_key(name: str, vendor: str) -> bool:
    """XIAOMI_API_KEY for Xiaomi, not GITHUB_TOKEN for GitHub Copilot or INFER_API_KEY."""
    stem = re.sub(r"_(API_KEY|APIKEY|API_TOKEN|TOKEN|KEY)$", "", name)
    if GENERIC_KEY.search(stem) or stem in {"INFER", "INFERENCE", "AI", "LLM", "GITHUB", "TOKEN"}:
        return False
    token = stem.lower().replace("_", "")
    return len(token) >= 3 and (token in vendor or vendor.startswith(token))


def _endpoint(entry: dict) -> str | None:
    """The API host, plus its path when the host is the vendor's main web domain:
    `ollama.com/v1` is the cloud API, a bare `ollama.com` is also every download link."""
    host = entry["host"]
    if not host:
        return None
    if host == registered_domain(host) or host.startswith("www."):
        path = re.sub(r"^https?://[^/]+", "", entry["api"] or "").rstrip("/")
        return f"{host}{path}" if path else None
    return host


# Words that name a kind of product, not a vendor: sharing one is not an overlap.
COMMON_WORDS = {
    "api", "apis", "ai", "llm", "llms", "cloud", "inference", "platform", "model", "models",
    "open", "code", "coding", "plan", "gateway", "router", "labs", "lab", "service",
    "services", "hosted", "studio", "chat", "search", "global", "token", "tokens", "the",
    "and", "for", "web", "endpoints", "serving", "compute", "network", "provider", "engine",
    "com", "net", "org", "dev", "app", "www", "run", "pass", "plus", "pro", "new", "one",
    "providers", "inc", "corp", "group", "systems", "technologies", "tech",
}  # fmt: skip


def _words(text: str) -> set[str]:
    return {
        w
        for w in re.split(r"[^a-z0-9]+", text.lower())
        if len(w) >= 3 and w not in COMMON_WORDS and not w.isdigit()
    }


def catalog_entries(
    missing: list[dict], taken: set[str], hand: list | None = None
) -> tuple[list[dict], list[tuple[dict, str]]]:
    """One entry per vendor: providers on the same registered domain (Xiaomi and its
    token plans) or sharing a key are one service with several endpoints. A provider
    whose name matches a hand-written entry (watsonx, Kimi) is a new endpoint of a
    vendor the catalog knows: it is returned apart, for a person to add there."""
    vendors: dict[str, set[str]] = defaultdict(set)
    hand_words = {s.id: _words(f"{s.id} {s.name}") for s in hand or []}
    for sid, words in hand_words.items():
        for w in words:
            vendors[w].add(sid)
    groups: dict[str, list[dict]] = defaultdict(list)
    for m in missing:
        key = registered_domain(m["host"]) if m["host"] else (m["env"] or [m["id"]])[0]
        groups[key].append(m)
    entries, overlaps = [], []
    for members in groups.values():
        members.sort(key=lambda m: (len(m["id"]), m["id"]))
        words = {w for m in members for w in _words(f"{m['id']} {m['name']}")}
        matches = {sid for w in words for sid in vendors.get(w, ())}
        # the hand entry sharing the most words: Cloudflare AI Gateway, not Sandbox
        known = max(sorted(matches), key=lambda sid: len(words & hand_words[sid]), default=None)
        if known:
            overlaps.extend((m, known) for m in members)
            continue
        members.sort(key=lambda m: (len(m["id"]), m["id"]))
        head = members[0]
        vendor = re.sub(r"[^a-z0-9]", "", f"{head['name']}{head['id']}".lower())
        if head["host"]:
            vendor += _label_of(head["host"])
        detect: dict[str, list[str]] = {}
        npm = sorted({m["npm"] for m in members if m["npm"]})
        endpoints = sorted({e for m in members if (e := _endpoint(m))})
        env = sorted({k for m in members for k in m["env"] if _vendor_key(k, vendor)})
        if npm:
            detect["npm"] = npm
        if endpoints:
            detect["endpoint"] = endpoints
        if env:
            detect["env"] = env
        if not detect.get("endpoint") and not detect.get("npm"):
            continue  # a key alone is too weak to name a service
        sid = head["id"] if head["id"] not in taken else f"{head['id']}-api"
        if sid in taken:
            continue
        taken.add(sid)
        name = re.sub(r"\s*\((?:[^)]*)\)$", "", head["name"]).strip()
        gateway = re.search(r"router|gateway|proxy|aihub", f"{name} {head['id']}", re.I)
        entry = {
            "id": sid,
            "name": name,
            "category": "LLM gateway" if gateway else "LLM API",
            "replace_with": ["llm-gateway"] if gateway else ["open-llm", "llm-serving"],
            "source": "models.dev",
            "doc": head["doc"],
        }
        if all(m["compatible"] for m in members):
            # In a file that names this provider's host, the openai import calls it.
            entry["excludes"] = ["openai"]
        entry["detect"] = detect
        entries.append(entry)
    return sorted(entries, key=lambda e: e["id"]), overlaps


def _label_of(host: str) -> str:
    return registered_domain(host).split(".")[0].replace("-", "")


def draft(entry: dict) -> dict:
    detect: dict[str, list[str]] = {}
    if entry["npm"]:
        detect["npm"] = [entry["npm"]]
    if entry["host"]:
        detect["endpoint"] = [entry["host"]]
    if entry["env"]:
        detect["env"] = entry["env"]
    return {
        "id": entry["id"],
        "name": entry["name"],
        "category": "LLM API",
        "replace_with": ["open-llm", "llm-serving"],
        "detect": detect,
    }


def corpus_candidates(dirs: list[Path]) -> list[dict]:
    seen: dict[str, dict] = {}
    repos: dict[str, set[str]] = defaultdict(set)
    for d in dirs:
        for report in sorted(d.glob("*.json")):
            try:
                data = json.loads(report.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for c in data.get("unknown_candidates") or []:
                if c.get("kind") == "provider catalog" or c.get("own_project"):
                    continue
                repos[c["name"]].add(data.get("scanned") or report.stem)
                seen.setdefault(c["name"], c)
    out = []
    for name, c in seen.items():
        first = c["evidence"][0]
        out.append(
            {
                "name": name,
                "repos": sorted(repos[name]),
                "hosts": c.get("hosts", []),
                "settings": c.get("settings", []),
                "example": f"{first['file']}:{first['line']}",
            }
        )
    out.sort(key=lambda x: (-len(x["repos"]), x["name"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--models-dev", action="store_true")
    ap.add_argument("--reports", type=Path, nargs="*", default=[])
    ap.add_argument("--drafts", type=Path)
    ap.add_argument(
        "--write-catalog",
        action="store_true",
        help=f"regenerate {GENERATED.relative_to(ROOT).as_posix()} from models.dev",
    )
    ap.add_argument("--catalog", type=Path, default=ROOT / "catalog")
    a = ap.parse_args()
    if not a.models_dev and not a.reports:
        ap.error("give --models-dev and/or --reports")
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    catalog = _hand_catalog(a.catalog)
    out = ["# AI services not in the catalog", ""]

    if a.models_dev:
        missing = models_dev_missing(catalog)
        out += [
            f"## Hosted providers on models.dev ({len(missing)})",
            "",
            "No host, key or package of these providers is in the catalog.",
            "",
        ]
        if missing:
            out += ["| Provider | API host | Key | npm | Models |", "|---|---|---|---|---|"]
            for m in missing:
                name = f"[{m['name']}]({m['doc']})" if m["doc"] else m["name"]
                keys = ", ".join(f"`{k}`" for k in m["env"]) or "—"
                out.append(
                    f"| {name} | {m['host'] or '—'} | {keys} | "
                    f"{'`' + m['npm'] + '`' if m['npm'] else '—'} | {m['models']} |"
                )
            out.append("")
        if a.write_catalog:
            hand = list(catalog.services)  # the hand catalog: generated file left out
            entries, overlaps = catalog_entries(missing, {s.id for s in hand}, hand)
            if overlaps:
                out += [
                    "### New endpoints of vendors the catalog knows",
                    "",
                    "Add these by hand to the entry named; they are not generated.",
                    "",
                ]
                for m, sid in overlaps:
                    keys = ", ".join(f"`{k}`" for k in m["env"]) or "—"
                    out.append(f"- `{sid}`: {m['name']} — {m['host'] or m['npm']} · {keys}")
                out.append("")
            GENERATED.write_text(
                "# Generated by scripts/new_services.py --models-dev --write-catalog from\n"
                "# https://models.dev (hosted model providers). Do not edit: a hand-written\n"
                "# entry in another file that shares a host, key or package replaces the\n"
                "# generated one at the next refresh.\n"
                + yaml.safe_dump(entries, sort_keys=False, allow_unicode=True, width=100),
                encoding="utf-8",
            )
            where = GENERATED.relative_to(ROOT).as_posix()
            out += [f"Wrote {len(entries)} entries to `{where}`.", ""]
        if a.drafts:
            body = {"new_services": [draft(m) for m in missing]}
            a.drafts.write_text(
                "# Drafts from models.dev: review id, category and signatures before\n"
                "# moving an entry into catalog/services/. Check packages with\n"
                f"#   python scripts/verify_packages.py --no-catalog --extra {a.drafts.name}\n"
                + yaml.safe_dump(body, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )

    if a.reports:
        found = corpus_candidates(a.reports)
        out += [
            f"## Candidates in scanned repos ({len(found)})",
            "",
            "`unknown_candidates` from unrent's own reports, most widely used first.",
            "",
        ]
        if found:
            out += ["| Service | Repos | Hosts and keys | Example |", "|---|---|---|---|"]
            for f in found:
                named = ", ".join([*f["hosts"], *(f"`{s}`" for s in f["settings"])])
                out.append(f"| {f['name']} | {len(f['repos'])} | {named} | `{f['example']}` |")
            out.append("")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
