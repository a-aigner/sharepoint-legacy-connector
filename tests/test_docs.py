"""Documentation that cannot silently rot.

Docs drift from code the moment nobody checks. These tests assert the specific
claims most likely to go stale: the settings table, the CLI command list, the
landing-zone field list, and the cross-links between documents.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from spconnect.config import Settings
from spconnect.models import AttachmentRecord, ItemRecord, Manifest

DOCS = Path(__file__).resolve().parent.parent / "docs"
README = Path(__file__).resolve().parent.parent / "README.md"

EXPECTED_DOCS = {
    "operations.md",
    "configuration.md",
    "landing-zone.md",
    "troubleshooting.md",
    "architecture.md",
    "security.md",
    "decisions.md",
}


def read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def all_docs() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in DOCS.glob("*.md")}


# --------------------------------------------------------------------------- #
# presence
# --------------------------------------------------------------------------- #


def test_every_expected_document_exists() -> None:
    assert {p.name for p in DOCS.glob("*.md")} >= EXPECTED_DOCS


def test_readme_links_to_each_document() -> None:
    readme = README.read_text(encoding="utf-8")
    for name in EXPECTED_DOCS:
        assert f"docs/{name}" in readme, f"README does not link to {name}"


def test_internal_links_resolve(all_docs: dict[str, str]) -> None:
    """No dangling `[text](other.md)` links between documents."""
    broken: list[str] = []
    for name, text in all_docs.items():
        for target in re.findall(r"\]\(([a-z0-9-]+\.md)(?:#[^)]*)?\)", text):
            if target not in all_docs:
                broken.append(f"{name} -> {target}")
    assert not broken, f"broken doc links: {broken}"


def test_anchor_links_resolve(all_docs: dict[str, str]) -> None:
    """`[text](doc.md#anchor)` must point at a heading that exists."""

    def anchors(text: str) -> set[str]:
        found = set()
        for heading in re.findall(r"^#{1,6}\s+(.+?)\s*$", text, re.MULTILINE):
            slug = re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-")
            found.add(slug)
        return found

    known = {name: anchors(text) for name, text in all_docs.items()}
    broken: list[str] = []
    for name, text in all_docs.items():
        for target, anchor in re.findall(r"\]\(([a-z0-9-]+\.md)#([^)]+)\)", text):
            if target in known and anchor not in known[target]:
                broken.append(f"{name} -> {target}#{anchor}")
    assert not broken, f"broken anchors: {broken}"


# --------------------------------------------------------------------------- #
# configuration.md tracks the Settings model
# --------------------------------------------------------------------------- #


def test_every_setting_is_documented() -> None:
    text = read("configuration.md")
    missing = [f"SP_{n.upper()}" for n in Settings.model_fields if f"`SP_{n.upper()}`" not in text]
    assert not missing, (
        f"undocumented settings: {missing}. Add them to docs/configuration.md — "
        "it is generated from the Settings model, so it must be regenerated when one is added."
    )


def test_no_settings_are_documented_that_do_not_exist() -> None:
    text = read("configuration.md")
    real = {f"SP_{n.upper()}" for n in Settings.model_fields}
    documented = set(re.findall(r"`(SP_[A-Z0-9_]+)`", text))
    # SP_LIVE_TESTS appears as an env var in prose too; both are real.
    assert not (documented - real), f"documented but nonexistent: {documented - real}"


def test_documented_defaults_match_the_model() -> None:
    text = read("configuration.md")
    checked = 0
    for name, field in Settings.model_fields.items():
        if hasattr(field.default, "get_secret_value"):
            continue
        row = re.search(rf"^\| `SP_{name.upper()}` \|[^|]*\| `([^`]*)` \|", text, re.MULTILINE)
        if row is None:
            continue
        documented = row.group(1)
        actual = str(field.default) if str(field.default) != "" else "(empty)"
        assert documented == actual, f"SP_{name.upper()}: doc says {documented!r}, model says {actual!r}"
        checked += 1
    assert checked > 20, "the defaults table stopped being parsed — check its format"


def test_the_recommended_auth_mode_is_stated_accurately() -> None:
    # The built-in default is `ntlm`; .env.example ships `integrated`. Claiming
    # the default is `integrated` would be wrong and would confuse debugging.
    assert Settings.model_fields["auth_mode"].default == "ntlm"
    config = read("configuration.md")
    assert "built-in default stays `ntlm`" in config
    assert "`integrated` is recommended" in config


# --------------------------------------------------------------------------- #
# landing-zone.md tracks the on-disk models
# --------------------------------------------------------------------------- #


def test_every_item_field_is_documented() -> None:
    text = read("landing-zone.md")
    missing = [name for name in ItemRecord.model_fields if f"`{name}`" not in text]
    assert not missing, f"undocumented items.jsonl fields: {missing}"


def test_every_attachment_field_is_documented() -> None:
    text = read("landing-zone.md")
    missing = [name for name in AttachmentRecord.model_fields if f'"{name}"' not in text]
    assert not missing, f"undocumented attachment fields: {missing}"


def test_manifest_keys_the_reader_depends_on_are_documented() -> None:
    text = read("landing-zone.md")
    for key in ("counts", "errors", "warnings", "lists_with_unique_scopes", "api_mode"):
        assert key in Manifest.model_fields
        assert f"`{key}" in text, f"manifest key {key} not documented"


def test_the_landing_zone_example_reader_is_valid_python() -> None:
    text = read("landing-zone.md")
    blocks = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
    assert blocks, "the reader example disappeared from landing-zone.md"
    for block in blocks:
        compile(block, "<landing-zone.md>", "exec")


def test_every_decoded_type_in_the_dispatch_table_is_documented() -> None:
    from spconnect.decode import _SIMPLE_DECODERS

    text = read("landing-zone.md")
    # Aliases we deliberately do not spell out one by one.
    internal = {"Guid", "ContentTypeId", "AllDayEvent", "GridChoice", "ModStat", "Likes"}
    missing = [t for t in _SIMPLE_DECODERS if t not in internal and f"`{t}`" not in text]
    assert not missing, f"decoded types missing from the value-shapes table: {missing}"


# --------------------------------------------------------------------------- #
# operations.md tracks the CLI
# --------------------------------------------------------------------------- #


def test_every_command_is_documented() -> None:
    from spconnect.cli import app

    text = read("operations.md")
    names = {c.name or (c.callback.__name__ if c.callback else "") for c in app.registered_commands}
    for name in names:
        assert name and f"spconnect {name}" in text, f"command {name!r} not in operations.md"


def test_the_escalation_ladder_is_in_order() -> None:
    """The runbook only works if each rung precedes the one that depends on it."""
    full = read("operations.md")
    ladder = full[full.index("## 3. The escalation ladder") : full.index("## 4. Commands")]
    rungs = re.findall(r"^### Rung (\d+)", ladder, re.MULTILINE)
    assert rungs == sorted(rungs, key=int), f"rungs out of order: {rungs}"
    assert len(rungs) >= 8, f"expected the full ladder, found {len(rungs)} rungs"

    order = [
        "spconnect probe",
        "SP_LIVE_TESTS=1",
        "spconnect discover",
        "spconnect schema",
        "--dry-run",
        "verify-time",
        "--include-lists",
        "spconnect crawl\n",
    ]
    positions = [ladder.index(token) for token in order]
    assert positions == sorted(positions), "the ladder steps are out of order"


def test_auth_modes_are_all_documented() -> None:
    from typing import get_args

    from spconnect.config import AuthMode

    text = read("security.md")
    for mode in get_args(AuthMode):
        assert f"`{mode}`" in text, f"auth mode {mode} not covered in security.md"


def test_skip_reasons_are_documented() -> None:
    text = read("landing-zone.md")
    for reason in ("downloads_disabled", "extension_excluded", "too_large", "download_failed"):
        assert reason in text, f"skip reason {reason} not documented"


# --------------------------------------------------------------------------- #
# the promises are the ones the code actually makes
# --------------------------------------------------------------------------- #


def test_the_flattened_permissions_warning_appears_everywhere_it_matters() -> None:
    # The most consequential caveat in the whole project. It must not be
    # findable only by someone who reads all seven documents.
    for name in ("landing-zone.md", "security.md"):
        assert "item-level" in read(name).lower(), f"{name} omits the permissions caveat"


def test_dateinutc_caveat_is_stated_where_the_data_is_described() -> None:
    text = read("landing-zone.md")
    assert "DateInUtc" in text
    assert "verify-time" in text


def test_docs_do_not_claim_rest_replaces_soap() -> None:
    # A recurring misreading worth pinning: OData is a second item source, not
    # an alternative stack.
    architecture = read("architecture.md")
    assert "not a replacement" in architecture
    assert "no change feed" in architecture
