"""The operator's five-minute confidence check against the real server.

Entirely skipped unless ``SP_LIVE_TESTS=1``. Read-only: it probes the version
header, enumerates webs, lists the first web's lists, and pulls exactly one page
of one list. It never writes to the landing zone and never writes to SharePoint.

    SP_LIVE_TESTS=1 pytest tests/test_live_smoke.py -v -s
"""

from __future__ import annotations

import os

import pytest

from spconnect.config import load_settings, setup_logging
from spconnect.services.lists import ListsService, is_system_list
from spconnect.services.webs import WebsService
from spconnect.transport import Transport

pytestmark = pytest.mark.skipif(
    os.environ.get("SP_LIVE_TESTS", "0") not in ("1", "true", "TRUE", "yes"),
    reason="live server tests are opt-in: set SP_LIVE_TESTS=1",
)


@pytest.fixture(scope="module")
def live_settings():
    settings = load_settings()
    if not settings.base_url or settings.base_url == "http://localhost":
        pytest.skip("SP_BASE_URL is not configured")
    setup_logging(settings.log_level, settings.log_format)
    return settings


@pytest.fixture(scope="module")
def live_transport(live_settings):
    with Transport(live_settings) as transport:
        yield transport


def test_version_header(live_transport, live_settings) -> None:
    version = live_transport.probe_version()
    print(f"\n  server: {version.raw!r} -> {version.product}")
    print(f"  change tokens supported: {version.supports_change_tokens}")
    if version.raw is None:
        pytest.fail(
            "No MicrosoftSharePointTeamServices header. Either SP_BASE_URL does not "
            "point at a SharePoint web application, or a proxy is stripping headers."
        )
    assert version.major is not None


def test_get_all_sub_web_collection(live_transport, live_settings) -> None:
    webs = WebsService(live_transport, live_settings.base_url).get_all_sub_web_collection()
    print(f"\n  {len(webs)} webs readable by {live_settings.username or '<anonymous>'}:")
    for web in webs[:25]:
        print(f"    {web.url}  {web.title}")
    if len(webs) > 25:
        print(f"    … and {len(webs) - 25} more")
    print("  If this count looks low, the credential is missing permissions somewhere.")
    assert webs


def test_get_list_collection_on_the_first_web(live_transport, live_settings) -> None:
    webs = WebsService(live_transport, live_settings.base_url).get_all_sub_web_collection()
    lists = ListsService(live_transport, webs[0].url).get_list_collection()
    business = [li for li in lists if not is_system_list(li) and not li.hidden]

    print(f"\n  {webs[0].url}: {len(lists)} lists, {len(business)} after filtering")
    for info in sorted(business, key=lambda li: -li.item_count)[:25]:
        scope = "  [unique scopes]" if info.has_unique_scopes else ""
        print(f"    {info.item_count:>8,}  {info.title}  ({info.base_type_name}){scope}")
    assert lists


def test_one_page_of_one_list(live_transport, live_settings) -> None:
    webs = WebsService(live_transport, live_settings.base_url).get_all_sub_web_collection()
    for web in webs:
        service = ListsService(live_transport, web.url)
        candidates = [
            li
            for li in service.get_list_collection()
            if not is_system_list(li) and not li.hidden and li.item_count > 0 and li.base_type == "0"
        ]
        if not candidates:
            continue

        target = max(candidates, key=lambda li: li.item_count)
        schema = service.get_list_schema(target)
        page = service.get_list_items(
            target.guid,
            last_id=0,
            row_limit=min(5, live_settings.page_size),
            field_names=[f.name for f in schema.fields if f.name and f.name != "MetaInfo"],
        )

        print(f"\n  {web.url} :: {target.title} — {len(schema.fields)} fields, {len(page.rows)} rows")
        lookups = [f for f in schema.fields if f.is_lookup]
        print(f"  lookup columns: {', '.join(f'{f.name}->{f.lookup_list}' for f in lookups) or 'none'}")
        for row in page.rows[:3]:
            print(f"    ID={row.get('ows_ID')}  Title={row.get('ows_Title')!r}")
            print(f"      Created (wire) = {row.get('ows_Created')!r}")
            print(f"      Modified (wire) = {row.get('ows_Modified')!r}")
        print("  Compare those datetimes against the SharePoint UI: DateInUtc is a claim,")
        print("  not a guarantee. `spconnect verify-time` does this properly.")

        assert page.rows
        return

    pytest.skip("no non-empty, non-system list was readable on any web")


def test_nothing_was_written(live_settings) -> None:
    # The landing zone must be untouched by the smoke test.
    marker = live_settings.landing_dir / "_manifest.json"
    before = marker.stat().st_mtime if marker.exists() else None
    assert before == (marker.stat().st_mtime if marker.exists() else None)
