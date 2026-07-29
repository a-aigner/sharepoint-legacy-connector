"""``Webs.GetAllSubWebCollection`` — discovery, normalisation, dedup."""

from __future__ import annotations

import pytest

from conftest import WEB1, WEB2, FakeFarm
from spconnect.models import normalise_url, web_id_for
from spconnect.services.webs import WebsService
from spconnect.transport import Transport


@pytest.fixture
def webs(farm: FakeFarm, transport: Transport):
    return WebsService(transport, WEB1).get_all_sub_web_collection()


def test_discovers_every_readable_web(webs) -> None:
    assert [w.url for w in webs] == [WEB1, WEB2]


def test_titles_are_kept(webs) -> None:
    assert {w.url: w.title for w in webs} == {WEB1: "Service", WEB2: "Fälle 2008"}


def test_urls_are_normalised_and_deduplicated(webs) -> None:
    # The fixture repeats the root web with a trailing slash and an uppercase host.
    assert len(webs) == 2
    assert all(not w.url.endswith("/") for w in webs)
    assert all(w.url.startswith("http://sp/") for w in webs)


def test_the_called_web_is_included_even_if_the_server_omits_it(farm: FakeFarm, transport: Transport) -> None:
    webs = WebsService(transport, WEB2).get_all_sub_web_collection()
    assert WEB2 in [w.url for w in webs]


def test_web_ids_are_stable_and_distinct(webs) -> None:
    assert webs[0].web_id != webs[1].web_id
    assert webs[0].web_id == web_id_for(WEB1)
    # Stability is the whole point: doc_id is built from this.
    assert web_id_for(WEB1) == web_id_for(WEB1 + "/") == web_id_for("http://SP/sites/service")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://SP/sites/Service/", "http://sp/sites/Service"),
        ("http://sp/sites/service", "http://sp/sites/service"),
        ("  http://sp/  ", "http://sp"),
        ("/sites/service/", "/sites/service"),
    ],
)
def test_normalise_url_lowercases_the_host_and_keeps_path_case(raw: str, expected: str) -> None:
    assert normalise_url(raw) == expected


def test_endpoint_is_built_per_web(transport: Transport) -> None:
    assert WebsService(transport, WEB2).client.endpoint == f"{WEB2}/_vti_bin/Webs.asmx"
