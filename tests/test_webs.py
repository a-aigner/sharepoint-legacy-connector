"""``Webs.GetAllSubWebCollection`` — discovery, normalisation, dedup."""

from __future__ import annotations

import pytest

from conftest import WEB1, WEB2, FakeFarm
from spconnect.models import WebRef, normalise_url, web_id_for
from spconnect.services.webs import WebsService
from spconnect.soap import SharePointSoapFault, SoapResponseError
from spconnect.transport import AuthenticationError, Transport


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


# --------------------------------------------------------------------------- #
# WSS 2.0 fallback — GetAllSubWebCollection does not exist before WSS 3.0
# --------------------------------------------------------------------------- #


def test_prefers_the_single_recursive_call_when_available(farm: FakeFarm, transport: Transport) -> None:
    discovery = WebsService(transport, WEB1).discover_all_webs()
    assert discovery.method == "GetAllSubWebCollection"
    assert [w.url for w in discovery.webs] == [WEB1, WEB2]
    assert discovery.warnings == []
    assert discovery.complete
    assert farm.count("GetWebCollection") == 0


def test_walks_with_get_web_collection_on_a_pre_wss3_server(farm: FakeFarm, transport: Transport) -> None:
    discovery = WebsService(transport, WEB1).discover_all_webs(prefer_recursive_call=False)

    assert discovery.method == "GetWebCollection"
    assert [w.url for w in discovery.webs] == [WEB1, WEB2]
    assert farm.count("GetAllSubWebCollection") == 0
    # Root, then its one child — the child reports no children of its own.
    assert farm.count("GetWebCollection") == 2
    assert any("predates GetAllSubWebCollection" in w for w in discovery.warnings)


def test_falls_back_when_the_server_faults_on_the_recursive_call(
    farm: FakeFarm, transport: Transport
) -> None:
    farm.always_fail["GetAllSubWebCollection"] = "soap_fault_unknown_action.xml"

    discovery = WebsService(transport, WEB1).discover_all_webs()

    assert discovery.method == "GetWebCollection"
    assert [w.url for w in discovery.webs] == [WEB1, WEB2]
    assert any("falling back" in w for w in discovery.warnings)


def test_falls_back_when_the_server_returns_a_login_page(farm: FakeFarm, transport: Transport) -> None:
    # HTTP 200 with an FBA login page instead of SOAP — the failure the operator hit.
    farm.always_fail["GetAllSubWebCollection"] = "html_login_page.html"

    discovery = WebsService(transport, WEB1).discover_all_webs()

    assert discovery.method == "GetWebCollection"
    assert [w.url for w in discovery.webs] == [WEB1, WEB2]


def test_the_walk_keeps_titles(farm: FakeFarm, transport: Transport) -> None:
    discovery = WebsService(transport, WEB1).discover_all_webs(prefer_recursive_call=False)
    assert {w.url: w.title for w in discovery.webs}[WEB2] == "Fälle 2008"


def test_the_walk_survives_an_unreadable_subweb(farm: FakeFarm, transport: Transport) -> None:
    calls: list[str] = []
    original = WebsService.get_web_collection

    def flaky(self):  # the child web 401s for everyone except the crawl account
        calls.append(self.web_url)
        if self.web_url == WEB2:
            raise SharePointSoapFault("GetWebCollection", "access denied")
        return original(self)

    WebsService.get_web_collection = flaky
    try:
        discovery = WebsService(transport, WEB1).discover_all_webs(prefer_recursive_call=False)
    finally:
        WebsService.get_web_collection = original

    assert [w.url for w in discovery.webs] == [WEB1, WEB2]  # the child itself is still known
    assert discovery.unreadable == [WEB2]
    assert not discovery.complete  # and we say so, rather than implying completeness


def test_the_walk_does_not_loop_on_a_self_referencing_web(farm: FakeFarm, transport: Transport) -> None:
    original = WebsService.get_web_collection

    def cyclic(self):  # a server that returns the called web as its own child
        return [WebRef(title="Service", url=WEB1), WebRef(title="Fälle 2008", url=WEB2)]

    WebsService.get_web_collection = cyclic
    try:
        discovery = WebsService(transport, WEB1).discover_all_webs(prefer_recursive_call=False)
    finally:
        WebsService.get_web_collection = original

    assert [w.url for w in discovery.webs] == [WEB1, WEB2]


def test_the_walk_stops_at_the_depth_limit(farm: FakeFarm, transport: Transport) -> None:
    original = WebsService.get_web_collection

    def infinite(self):  # every web claims one new deeper child, forever
        depth = self.web_url.count("/sub")
        return [WebRef(title=f"L{depth + 1}", url=f"{self.web_url}/sub")]

    WebsService.get_web_collection = infinite
    try:
        discovery = WebsService(transport, WEB1).discover_all_webs(prefer_recursive_call=False, max_depth=4)
    finally:
        WebsService.get_web_collection = original

    assert len(discovery.webs) == 5  # root + 4 levels
    assert any("stopped at depth 4" in w for w in discovery.warnings)


def test_auth_failure_during_the_walk_is_not_swallowed(farm: FakeFarm, transport: Transport) -> None:
    original = WebsService.get_web_collection

    def unauthorised(self):
        raise AuthenticationError("HTTP 401")

    WebsService.get_web_collection = unauthorised
    try:
        with pytest.raises(AuthenticationError):
            WebsService(transport, WEB1).discover_all_webs(prefer_recursive_call=False)
    finally:
        WebsService.get_web_collection = original


def test_a_failure_on_the_root_web_is_fatal_not_a_partial_result(
    farm: FakeFarm, transport: Transport
) -> None:
    # Reporting "1 web, complete=False" here would dress up total failure as
    # near-success, and the operator would crawl an almost-empty farm.
    farm.always_fail["GetAllSubWebCollection"] = "html_login_page.html"
    farm.always_fail["GetWebCollection"] = "html_login_page.html"

    with pytest.raises(SoapResponseError) as excinfo:
        WebsService(transport, WEB1).discover_all_webs()

    assert "forms-authentication login page" in str(excinfo.value)
