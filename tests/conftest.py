"""Offline test harness.

There is no live server during development, so every test replays hand-written
SOAP fixtures through ``responses``. :class:`FakeFarm` dispatches on the SOAP
operation and the parameters inside the envelope, which means the tests also
assert that the *request* we build is well-formed — a mock that ignored the body
would happily pass a crawler that sent nonsense.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

import pytest
import responses
from lxml import etree

from spconnect.config import Settings, setup_logging
from spconnect.landing import LandingZone
from spconnect.soap import find_all, find_one
from spconnect.state import StateStore
from spconnect.transport import Transport

FIXTURES = Path(__file__).parent / "fixtures"

WEB1 = "http://sp/sites/service"
WEB2 = "http://sp/sites/service/cases2008"

CASES = "{11111111-1111-1111-1111-111111111111}"
KUNDEN = "{22222222-2222-2222-2222-222222222222}"
DOKUMENTE = "{33333333-3333-3333-3333-333333333333}"
CASES2 = "{55555555-5555-5555-5555-555555555555}"
KUNDEN2 = "{66666666-6666-6666-6666-666666666666}"
DOKUMENTE2 = "{77777777-7777-7777-7777-777777777777}"
DANGLING = "{99999999-9999-9999-9999-999999999999}"

SERVER_VERSION_HEADER = {"MicrosoftSharePointTeamServices": "12.0.0.6421"}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _substitute(text: str, replacements: dict[str, str]) -> str:
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


class ParsedRequest:
    """The interesting parts of an outgoing SOAP envelope."""

    def __init__(self, body: bytes, soap_action: str | None) -> None:
        self.body = body
        self.soap_action = (soap_action or "").strip('"')
        self.operation = self.soap_action.rsplit("/", 1)[-1]
        self.root = etree.fromstring(body)

    def param(self, name: str) -> str | None:
        el = find_one(self.root, name)
        if el is None:
            return None
        return ("".join(el.itertext()) or "").strip() or None

    @property
    def list_name(self) -> str:
        return (self.param("listName") or "").upper()

    @property
    def last_id(self) -> int:
        for value in find_all(self.root, "Value"):
            if value.get("Type") == "Counter":
                try:
                    return int("".join(value.itertext()).strip())
                except ValueError:
                    return 0
        return 0

    @property
    def row_limit(self) -> int:
        try:
            return int(self.param("rowLimit") or "0")
        except ValueError:
            return 0

    @property
    def change_token(self) -> str | None:
        return self.param("changeToken")


class FakeFarm:
    """A two-web, six-list SharePoint 2007 farm made of XML files."""

    def __init__(self, mock: responses.RequestsMock) -> None:
        self.mock = mock
        self.requests: list[ParsedRequest] = []
        self.odata_requests: list[str] = []
        self.item_responses: dict[tuple[str, int], str] = {}
        self.changes_fixture = "lists_getlistitemchangessincetoken.xml"
        self.fail_on: dict[str, Exception | str] = {}
        #: Operations that always fail, e.g. to simulate a WSS 2.0 build where
        #: GetAllSubWebCollection does not exist.
        self.always_fail: dict[str, str] = {}
        #: ListData.svc behaviour: None = serve fixtures, or a fixture name to
        #: return instead (e.g. an HTML 404 for a farm without the feature).
        self.odata_broken: str | None = None
        self.odata_broken_status: int = 404
        self._install()

    # ---- routing tables ----

    SCHEMAS: ClassVar[dict[str, tuple[str, dict[str, str]]]] = {
        CASES: ("lists_getlist_cases.xml", {}),
        KUNDEN: ("lists_getlist_kunden.xml", {}),
        DOKUMENTE: ("lists_getlist_dokumente.xml", {}),
        CASES2: ("lists_getlist_cases.xml", {CASES: CASES2, "Servicefälle": "Servicefälle 2008"}),
        KUNDEN2: ("lists_getlist_kunden.xml", {KUNDEN: KUNDEN2, ">Kunden<": ">Kunden 2008<"}),
        DOKUMENTE2: ("lists_getlist_dokumente.xml", {DOKUMENTE: DOKUMENTE2}),
    }

    def items_fixture(self, list_guid: str, last_id: int) -> str:
        override = self.item_responses.get((list_guid, last_id))
        if override is not None:
            return override
        if list_guid in (CASES, CASES2):
            return {0: "lists_getlistitems_page1.xml", 2: "lists_getlistitems_page2.xml"}.get(
                last_id, "lists_getlistitems_empty.xml"
            )
        if list_guid in (DOKUMENTE, DOKUMENTE2):
            return "lists_getlistitems_dokumente.xml" if last_id == 0 else "lists_getlistitems_empty.xml"
        return "lists_getlistitems_empty.xml"

    # ---- wiring ----

    def _install(self) -> None:
        for web in (WEB1, WEB2):
            for service in ("Webs", "Lists", "SiteData", "UserGroup"):
                self.mock.add_callback(
                    responses.POST,
                    f"{web}/_vti_bin/{service}.asmx",
                    callback=self._dispatch,
                    content_type="text/xml; charset=utf-8",
                )
        # HEAD must not carry a body, or requests raises ChunkedEncodingError.
        self.mock.add(responses.HEAD, WEB1, status=200, headers=SERVER_VERSION_HEADER)
        self.mock.add(responses.GET, WEB1, status=200, headers=SERVER_VERSION_HEADER, body="<html/>")

        self.mock.add(
            responses.GET,
            "http://sp/sites/service/Lists/Cases/Attachments/1/foto.jpg",
            body=b"\xff\xd8\xff\xe0JPEG-ish bytes",
            status=200,
        )
        self._install_odata()

        for url, payload in {
            "http://sp/sites/service/Lists/Cases/Attachments/3/Prüfprotokoll.pdf": b"%PDF-1.4 protokoll",
            "http://sp/sites/service/Lists/Cases/Attachments/3/messwerte.csv": b"a;b\n1;2\n",
            "http://sp/sites/service/Dokumente/Handbuch%20Stra%C3%9Fe.pdf": b"%PDF-1.4 handbuch",
        }.items():
            self.mock.add(responses.GET, url, body=payload, status=200)

    def _install_odata(self) -> None:
        for web in (WEB1, WEB2):
            self.mock.add_callback(
                responses.GET,
                re.compile(re.escape(f"{web}/_vti_bin/ListData.svc") + r".*"),
                callback=self._dispatch_odata,
                content_type="application/json",
            )

    def _dispatch_odata(self, request: Any) -> tuple[int, dict[str, str], str]:
        url = request.url
        self.odata_requests.append(url)
        if self.odata_broken:
            body = fixture(self.odata_broken)
            return self.odata_broken_status, {"Content-Type": "text/html"}, body

        path = url.split("/ListData.svc", 1)[1]
        query = path.split("?", 1)[1] if "?" in path else ""
        entity = path.split("?", 1)[0].strip("/")

        if not entity:
            return 200, {"Content-Type": "application/json"}, fixture("odata_service_document.json")
        if "Servicef" not in entity:
            return 200, {"Content-Type": "application/json"}, fixture("odata_empty.json")
        # Page on the same Id filter the SOAP backend uses.
        match = re.search(r"Id%20gt%20(\d+)", query) or re.search(r"Id gt (\d+)", query)
        last_id = int(match.group(1)) if match else 0
        name = {0: "odata_cases_page1.json", 2: "odata_cases_page2.json"}.get(last_id, "odata_empty.json")
        return 200, {"Content-Type": "application/json"}, fixture(name)

    def _dispatch(self, request: Any) -> tuple[int, dict[str, str], str]:
        parsed = ParsedRequest(request.body, request.headers.get("SOAPAction"))
        self.requests.append(parsed)

        always = self.always_fail.get(parsed.operation)
        if always is not None:
            body = fixture(always) if always.endswith((".xml", ".html")) else always
            status = 200 if always.endswith(".html") else 500
            return status, {"Content-Type": "text/xml"}, body

        failure = self.fail_on.get(parsed.operation)
        if failure is not None:
            self.fail_on.pop(parsed.operation)
            if isinstance(failure, Exception):
                raise failure
            return 500, {"Content-Type": "text/xml"}, fixture(str(failure))

        web = request.url.split("/_vti_bin/")[0]
        body = self._respond(parsed, web)
        return 200, {"Content-Type": "text/xml; charset=utf-8"}, body

    def _respond(self, parsed: ParsedRequest, web: str) -> str:
        op = parsed.operation
        if op == "GetAllSubWebCollection":
            return fixture("webs_getallsubwebcollection.xml")
        if op == "GetWebCollection":
            # Immediate children only — the WSS 2.0 shape.
            if web == WEB1:
                return fixture("webs_getwebcollection_root.xml")
            return fixture("webs_getwebcollection_empty.xml")
        if op == "GetSiteAndWeb":
            return (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
                '<GetSiteAndWebResponse xmlns="http://schemas.microsoft.com/sharepoint/soap/">'
                "<GetSiteAndWebResult>0</GetSiteAndWebResult>"
                "</GetSiteAndWebResponse></soap:Body></soap:Envelope>"
            )
        if op == "GetListCollection":
            name = "lists_getlistcollection.xml" if web == WEB1 else "lists_getlistcollection_web2.xml"
            return fixture(name)
        if op == "GetList":
            name, subs = self.SCHEMAS[parsed.list_name]
            return _substitute(fixture(name), subs)
        if op == "GetListItems":
            return fixture(self.items_fixture(parsed.list_name, parsed.last_id))
        if op == "GetListItemChangesSinceToken":
            return fixture(self.changes_fixture)
        if op == "GetAttachmentCollection":
            return fixture("lists_getattachmentcollection.xml")
        if op == "GetGroupCollectionFromUser":
            return fixture("usergroup_groups.xml")
        if op == "GetRoleCollectionFromUser":
            return fixture("usergroup_roles.xml")
        raise AssertionError(f"unexpected SOAP operation: {op!r}")

    # ---- assertions helpers ----

    def operations(self) -> list[str]:
        return [r.operation for r in self.requests]

    def count(self, operation: str) -> int:
        return sum(1 for r in self.requests if r.operation == operation)


@pytest.fixture(autouse=True)
def _quiet_logging() -> None:
    setup_logging("CRITICAL", "console")


@pytest.fixture
def mocked_responses():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


@pytest.fixture
def farm(mocked_responses: responses.RequestsMock) -> FakeFarm:
    return FakeFarm(mocked_responses)


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "base_url": WEB1,
        "auth_mode": "anonymous",
        "username": "",
        "password": "",
        "allow_legacy_tls": False,
        "verify_ssl": False,
        "requests_per_second": 10_000.0,
        "max_retries": 2,
        "backoff_base_seconds": 0.001,
        "page_size": 2,
        "landing_dir": tmp_path / "landing",
        "state_file": tmp_path / "landing" / "_state.json",
        "log_level": "CRITICAL",
        "concurrency": 2,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def transport(settings: Settings) -> Transport:
    return Transport(settings)


@pytest.fixture
def landing(settings: Settings) -> LandingZone:
    return LandingZone(settings.landing_dir)


@pytest.fixture
def state(settings: Settings) -> StateStore:
    return StateStore(settings.state_file)
