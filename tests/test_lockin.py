"""Tests. The catalog is the product, so most of these guard the catalog's honesty."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lockin.catalog import load_catalog
from lockin.detect import collect_facts, match
from lockin.report import summarise, to_json, to_markdown

CATALOG_DIR = Path(__file__).resolve().parent.parent / "catalog"


@pytest.fixture(scope="session")
def catalog():
    return load_catalog(CATALOG_DIR)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "rag.py").write_text(
        "import os\n"
        "from openai import AzureOpenAI\n"
        "from pinecone import Pinecone\n"
        'client = AzureOpenAI(azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"])\n',
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text(
        "openai==1.51.0\npinecone-client==5.0.1\n", encoding="utf-8"
    )
    (tmp_path / "main.tf").write_text(
        'resource "azurerm_search_service" "s" {\n}\n', encoding="utf-8"
    )
    return tmp_path


# --- catalog integrity -----------------------------------------------------


def test_catalog_loads(catalog):
    assert len(catalog) > 0


def test_every_alternative_states_what_you_lose(catalog):
    """An alternative with no stated cost is advocacy, not assessment."""
    for entry in catalog.entries:
        for alt in entry.alternatives:
            assert len(alt.loses) > 20, f"{entry.id} → {alt.name}: 'loses' is too thin to be honest"


def test_every_assessed_entry_has_an_alternative(catalog):
    """An assessed entry names a way out. A detected one deliberately does not."""
    for entry in catalog.entries:
        if entry.tier != "assessed":
            continue
        assert entry.alternatives, f"{entry.id} has no alternatives"


def test_detected_entries_claim_nothing_they_cannot_support(catalog):
    """The tier exists so coverage can grow without the catalog inventing judgement."""
    for entry in catalog.entries:
        if entry.tier != "detected":
            continue
        assert entry.lockin == "unassessed"
        assert not entry.alternatives
        assert entry.assessment is None
        assert entry.binding, f"{entry.id} should still say what it binds you through"
        assert any(entry.detect.values()), f"{entry.id} has no detection signature"


def test_excludes_point_at_real_entries(catalog):
    for entry in catalog.entries:
        for other in entry.excludes:
            assert catalog.by_id(other), f"{entry.id} excludes unknown entry '{other}'"


def test_staleness_is_reported_not_enforced(catalog):
    """Staleness is a property to report, not an assertion to break builds with.

    This used to assert that no entry was stale, which made the suite go red on a
    fixed date with no code change — permanently red for anyone who forked the repo
    after it. The report already tells the reader how old an assessment is; that is
    where the information belongs.
    """
    entry = catalog.entries[0]
    assert isinstance(entry.age_days, int)
    assert isinstance(entry.is_stale, bool)


# --- detection -------------------------------------------------------------


def test_finds_dependencies_across_file_types(project, catalog):
    findings = match(collect_facts(project, catalog), catalog)
    found = {f.entry.id for f in findings}
    assert "pinecone" in found
    assert "azure.openai" in found
    assert "azure.ai-search" in found


def test_azure_openai_claims_the_shared_openai_import(project, catalog):
    """`AzureOpenAI` lives in the `openai` package; it must not be reported twice."""
    findings = match(collect_facts(project, catalog), catalog)
    assert "openai.api" not in {f.entry.id for f in findings}


def test_symbol_match_respects_identifier_boundaries(tmp_path, catalog):
    """`OpenAI(` must not match inside `AzureOpenAI(`."""
    (tmp_path / "a.py").write_text("x = AzureOpenAI()\n", encoding="utf-8")
    facts = collect_facts(tmp_path, catalog)
    assert not [f for f in facts if f.kind == "python_symbol" and f.value == "OpenAI("]


def test_every_finding_cites_evidence(project, catalog):
    for finding in match(collect_facts(project, catalog), catalog):
        for fact in finding.facts:
            assert fact.line > 0
            assert fact.evidence.strip(), "a finding that cannot cite its source does not exist"


def test_skips_vendored_directories(project, catalog):
    junk = project / "node_modules" / "pkg"
    junk.mkdir(parents=True)
    (junk / "x.py").write_text("import cohere\n", encoding="utf-8")
    findings = match(collect_facts(project, catalog), catalog)
    assert "cohere.api" not in {f.entry.id for f in findings}


def test_clean_project_reports_nothing(tmp_path, catalog):
    """A project with no AI dependency must report none.

    The old fixture was an *empty* project, not a clean one — nothing in it came
    near a signature, so the test could not fail. This one is ordinary code that
    happens to contain words the catalog once treated as vendor signatures:
    `upsert(` was a Pinecone signature, `api_version=` an Azure OpenAI one, and a
    bare `boto3` import meant AWS Bedrock. All three are ordinary Python.
    """
    (tmp_path / "db.py").write_text(
        "def upsert(conn, row):\n    conn.execute('INSERT ... ON CONFLICT', row)\n",
        encoding="utf-8",
    )
    (tmp_path / "client.py").write_text(
        "import requests\n\n\ndef call(api_version='2024-01-01'):\n"
        "    return requests.get('https://example.com')\n",
        encoding="utf-8",
    )
    (tmp_path / "upload.py").write_text(
        'import boto3\n\ns3 = boto3.client("s3")\n', encoding="utf-8"
    )
    found = match(collect_facts(tmp_path, catalog), catalog)
    assert found == [], f"named vendors that are not there: {[f.entry.name for f in found]}"


def test_scanning_a_repo_that_merely_mentions_vendors(tmp_path, catalog):
    """Documentation is not a dependency.

    lockin could not scan its own source tree without reporting twelve findings,
    because its catalog names the very signatures it searches for. Any repo with an
    ADR, a docs page or a prompt fixture has the same problem.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "adr-003.yaml").write_text(
        "title: Why we did not choose Azure AI Search\n"
        "considered:\n"
        "  - SearchClient and SemanticConfiguration looked promising\n"
        "  - bedrock_client was evaluated and rejected\n",
        encoding="utf-8",
    )
    found = match(collect_facts(tmp_path, catalog), catalog)
    assert found == [], f"mistook documentation for dependencies: {[f.entry.name for f in found]}"


def test_a_project_below_a_skipped_directory_name_is_still_scanned(tmp_path, catalog):
    """`build/myapp` is a project, not a build artefact.

    The skip list was matched against every ancestor of the scan root, so a checkout
    under any directory called build, target, env or dist reported itself clean.
    """
    root = tmp_path / "build" / "myapp"
    root.mkdir(parents=True)
    (root / "app.py").write_text("from openai import OpenAI\n\nc = OpenAI()\n", encoding="utf-8")
    found = {f.entry.id for f in match(collect_facts(root, catalog), catalog)}
    assert "openai.api" in found, "a real dependency was skipped because of an ancestor's name"


def test_inline_pyproject_dependencies_are_read(tmp_path, catalog):
    """`dependencies = ["openai"]` on one line is the form this project's own uses."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["openai>=1.0", "pinecone-client>=5"]\n',
        encoding="utf-8",
    )
    found = {f.entry.id for f in match(collect_facts(tmp_path, catalog), catalog)}
    assert {"openai.api", "pinecone"} <= found, f"missed inline dependencies, got {found}"


def test_requirement_names_are_normalised(tmp_path, catalog):
    """PEP 503: `huggingface-hub` and `huggingface_hub` are the same package."""
    (tmp_path / "requirements.txt").write_text("Pinecone_Client==5.0.1\n", encoding="utf-8")
    found = {f.entry.id for f in match(collect_facts(tmp_path, catalog), catalog)}
    assert "pinecone" in found


def test_a_malformed_package_json_does_not_kill_the_scan(tmp_path, catalog):
    """One bad file in a big tree must not take the run down."""
    (tmp_path / "package.json").write_text("[]", encoding="utf-8")
    (tmp_path / "app.py").write_text("from openai import OpenAI\n", encoding="utf-8")
    found = {f.entry.id for f in match(collect_facts(tmp_path, catalog), catalog)}
    assert "openai.api" in found


# --- reporting -------------------------------------------------------------


def test_reports_render(project, catalog):
    findings = match(collect_facts(project, catalog), catalog)
    md = to_markdown(findings, project, catalog)
    assert "What you lose" in md
    assert "Lock-in report" in md

    import json

    payload = json.loads(to_json(findings, project, catalog))
    assert payload["summary"]["dependencies_found"] == len(findings)
    assert all(f["evidence"] for f in payload["findings"])


def test_summary_counts_match(project, catalog):
    """Every finding lands in exactly one bucket, detected ones included.

    The old assertion left `unassessed` out, so it was false the moment a detected
    entry matched. It passed only because the fixture happened not to trip one.
    """
    (project / "extra.py").write_text("import langchain\n", encoding="utf-8")
    findings = match(collect_facts(project, catalog), catalog)
    s = summarise(findings)
    assert s["unassessed"] > 0, "fixture should trip a detected entry"
    assert s["locked"] + s["friction"] + s["portable"] + s["unassessed"] == s["dependencies_found"]


def test_plain_openai_is_not_reported_as_azure(tmp_path, catalog):
    """`excludes` must not let a specific entry claim evidence it cannot support.

    A codebase with nothing Azure in it must never be told it depends on Azure.
    """
    (tmp_path / "requirements.txt").write_text("openai==1.51.0\n", encoding="utf-8")
    found = {f.entry.id for f in match(collect_facts(tmp_path, catalog), catalog)}
    assert "azure.openai" not in found
    assert "openai.api" in found


# --- methodology (METHODOLOGY.md) ------------------------------------------


def test_every_label_follows_the_rubric(catalog):
    """The codebook is executable: labels are derived, not asserted."""
    for entry in catalog.entries:
        if entry.tier != "assessed":
            continue
        assert entry.assessment is not None, f"{entry.id} has no assessment axes"
        assert entry.assessment.label == entry.lockin, (
            f"{entry.id}: axes give '{entry.assessment.label}', entry says '{entry.lockin}'"
        )


def test_a_label_contradicting_its_axes_is_rejected(tmp_path):
    """An entry cannot quietly drift away from METHODOLOGY.md §3."""
    from lockin.catalog import CatalogError

    (tmp_path / "bad.yaml").write_text(
        "- id: x.y\n"
        "  name: X\n"
        "  category: test\n"
        "  lockin: locked\n"  # claims locked...
        "  assessment: {interface: 0, data: 0, behaviour: 0}\n"  # ...but axes say portable
        "  why: test\n"
        "  detect: {python_import: [xylophone]}\n"
        "  alternatives:\n"
        "    - name: A\n      kind: k\n      compat: c\n      effort: low\n"
        "      loses: something concrete that is long enough to count\n"
        "  verified: 2026-09-20\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="METHODOLOGY"):
        load_catalog(tmp_path)


def test_scan_is_deterministic(project, catalog):
    """Same code plus same catalog version must give byte-identical output."""
    first = to_json(match(collect_facts(project, catalog), catalog), project, catalog)
    second = to_json(match(collect_facts(project, catalog), catalog), project, catalog)
    import json

    a, b = json.loads(first), json.loads(second)
    a.pop("scanned_at"), b.pop("scanned_at")
    assert a == b


def test_an_unassessed_alternative_is_marked_as_such(catalog):
    """An alternative may name a candidate; the report must not pass it off as vetted.

    "Here is the open option, and nobody has verified it for you" is more use than
    silence, but only if the second half is said out loud.
    """
    gateway = catalog.by_id("vercel.ai-gateway")
    assert gateway, "the platform catalog should cover the gateway layer"
    litellm = next(a for a in gateway.alternatives if a.component == "litellm")
    assert not litellm.component_assessed

    findings = [type("F", (), {"entry": gateway, "facts": (), "cited": [], "confidence": "high"})()]
    md = to_markdown(findings, Path("/tmp"), catalog)
    assert "not assessed" in md


def test_the_catalog_covers_the_template_stack(catalog):
    """The most-forked AI starter scored zero until the platform layer existed.

    Most AI apps are not the enterprise RAG stack; they are a forked template, and
    the template brings a platform nobody chose.
    """
    for entry_id in ("vercel.ai-gateway", "vercel.blob", "vercel.platform"):
        assert catalog.by_id(entry_id), f"missing platform entry {entry_id}"


def test_something_portable_is_labelled_portable(catalog):
    """A scan that flags everything it recognises is one nobody believes twice."""
    sdk = catalog.by_id("vercel.ai-sdk")
    assert sdk and sdk.lockin == "portable"


def test_live_check_fails_soft(monkeypatch):
    """A scan is useful without the network.

    The tool's core promise is that nothing leaves the machine; it must keep working
    when nothing can. A registry lookup that fails is reported, never fatal, and
    never cached as though it were an answer.
    """
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from lockin import live

    def explode(url):
        raise OSError("no network")

    monkeypatch.setattr(live, "_get", explode)
    monkeypatch.setattr(live, "_load_cache", dict)
    monkeypatch.setattr(live, "_save_cache", lambda cache: None)

    facts = live.check({("pypi", "openai"), ("npm", "ai")})
    assert len(facts) == 2
    assert all(f.error for f in facts.values())
    assert not any(f.is_notable for f in facts.values())


def test_live_is_opt_in():
    """The default scan makes no network calls, so --live must be a flag."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from lockin.cli import build_parser

    args = build_parser().parse_args(["scan", "."])
    assert args.live is False


# --- the CLI contract -------------------------------------------------------


def test_fail_on_is_a_ci_contract(tmp_path):
    """`--fail-on` is documented as a CI gate and had no coverage at all."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from lockin.cli import main

    (tmp_path / "requirements.txt").write_text("pinecone-client==5.0.1\n", encoding="utf-8")
    out = tmp_path / "r.md"
    assert main(["scan", str(tmp_path), "-o", str(out)]) == 0
    assert main(["scan", str(tmp_path), "-o", str(out), "--fail-on", "friction"]) == 1
    assert main(["scan", str(tmp_path), "-o", str(out), "--fail-on", "locked"]) == 0


def test_bad_input_is_an_error_not_a_traceback(tmp_path):
    """A tool that tracebacks at a user is one they stop trusting."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from lockin.cli import main

    assert main(["scan", str(tmp_path / "nope")]) == 2
    assert main(["scan", str(tmp_path), "--catalog", str(tmp_path / "nope")]) == 2

    broken = tmp_path / "cat"
    broken.mkdir()
    (broken / "x.yaml").write_text("this: [is: not: valid\n", encoding="utf-8")
    assert main(["scan", str(tmp_path), "--catalog", str(broken)]) == 2

    (broken / "x.yaml").write_text("- just a string\n", encoding="utf-8")
    assert main(["scan", str(tmp_path), "--catalog", str(broken)]) == 2


def test_confidence_reflects_corroboration(project, catalog):
    """Four branches, none of them previously asserted."""
    findings = {f.entry.id: f for f in match(collect_facts(project, catalog), catalog)}

    # An import and a manifest entry are two independent kinds agreeing.
    assert {x.kind for x in findings["pinecone"].facts} >= {"python_import", "requirement"}
    assert findings["pinecone"].confidence == "high"

    # A Terraform resource alone is one strong kind and nothing corroborating it.
    assert {x.kind for x in findings["azure.ai-search"].facts} == {"terraform_resource"}
    assert findings["azure.ai-search"].confidence == "medium"

    # A lone environment variable is the weakest thing the catalog accepts.
    weak = [f for f in findings.values() if {x.kind for x in f.facts} == {"env"}]
    assert all(f.confidence == "low" for f in weak)


def test_a_docstring_mentioning_a_vendor_is_not_a_dependency(tmp_path, catalog):
    """Writing about a thing is not depending on it — but a client call still is."""
    (tmp_path / "notes.py").write_text(
        '"""We evaluated bedrock_client and SearchClient and chose neither."""\n'
        "# AzureOpenAI was also considered\n",
        encoding="utf-8",
    )
    assert match(collect_facts(tmp_path, catalog), catalog) == []

    (tmp_path / "real.py").write_text(
        'client = boto3.client("bedrock-runtime")\n', encoding="utf-8"
    )
    found = {f.entry.id for f in match(collect_facts(tmp_path, catalog), catalog)}
    assert "aws.bedrock" in found, "a string literal naming the service is real evidence"
