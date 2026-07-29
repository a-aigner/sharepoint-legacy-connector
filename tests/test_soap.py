"""Envelope construction, XML-fragment parameters, and fault handling."""

from __future__ import annotations

import pytest
from lxml import etree

from conftest import fixture_bytes
from spconnect.services.lists import id_page_query, query_options, view_fields
from spconnect.soap import (
    SP_SOAP_NS,
    SharePointSoapFault,
    SoapResponseError,
    build_envelope,
    diagnose_body,
    element,
    find_all,
    find_one,
    local_name,
    parse_fault,
    parse_response,
    soap_action,
)


def _parse(payload: bytes) -> etree._Element:
    return etree.fromstring(payload)


# --------------------------------------------------------------------------- #
# envelope
# --------------------------------------------------------------------------- #


def test_soap_action_is_the_namespace_plus_operation() -> None:
    assert soap_action("GetListItems") == "http://schemas.microsoft.com/sharepoint/soap/GetListItems"


def test_envelope_shape_and_namespaces() -> None:
    payload = build_envelope("GetListCollection")
    assert payload.startswith(b"<?xml version=")

    root = _parse(payload)
    assert root.tag == "{http://schemas.xmlsoap.org/soap/envelope/}Envelope"
    assert root.nsmap["xsi"] == "http://www.w3.org/2001/XMLSchema-instance"
    assert root.nsmap["xsd"] == "http://www.w3.org/2001/XMLSchema"

    body = root[0]
    assert local_name(body) == "Body"
    operation = body[0]
    assert operation.tag == f"{{{SP_SOAP_NS}}}GetListCollection"


def test_scalar_parameters_become_element_text() -> None:
    payload = build_envelope("GetList", {"listName": "{1111-2222}"})
    root = _parse(payload)
    assert find_one(root, "listName").text == "{1111-2222}"


def test_none_parameter_is_sent_as_an_empty_element() -> None:
    payload = build_envelope("GetListItems", {"listName": "x", "viewName": None})
    root = _parse(payload)
    view_name = find_one(root, "viewName")
    assert view_name is not None
    assert (view_name.text or "") == ""


def test_fragment_parameters_are_real_elements_not_escaped_strings() -> None:
    payload = build_envelope(
        "GetListItems",
        {
            "listName": "{1111}",
            "query": id_page_query(200),
            "viewFields": view_fields(["Title", "Kunde"]),
            "queryOptions": query_options(),
        },
    )
    # If these had been built by string concatenation they would arrive escaped.
    assert b"&lt;Query&gt;" not in payload
    assert b"&lt;QueryOptions&gt;" not in payload

    root = _parse(payload)
    query = find_one(root, "query")
    assert local_name(query[0]) == "Query"
    assert find_one(query, "Gt") is not None
    assert find_one(query, "FieldRef").get("Name") == "ID"
    assert find_one(query, "Value").text == "200"
    assert find_one(query, "Value").get("Type") == "Counter"

    order_by = find_one(query, "OrderBy")
    assert find_one(order_by, "FieldRef").get("Ascending") == "TRUE"

    fields = [el.get("Name") for el in find_all(find_one(root, "viewFields"), "FieldRef")]
    assert fields == ["Title", "Kunde"]


def test_query_options_always_request_utc_and_recursive_scope() -> None:
    payload = build_envelope("GetListItems", {"queryOptions": query_options()})
    root = _parse(payload)
    options = find_one(root, "QueryOptions")
    assert find_one(options, "DateInUtc").text == "TRUE"
    assert find_one(options, "IncludeMandatoryColumns").text == "TRUE"
    assert find_one(options, "IncludeAttachmentUrls").text == "TRUE"
    assert find_one(options, "ViewAttributes").get("Scope") == "RecursiveAll"


def test_user_supplied_values_are_escaped_by_the_serialiser() -> None:
    payload = build_envelope("GetList", {"listName": 'Kulanz & <Sonstiges> "x"'})
    assert b"Kulanz &amp; &lt;Sonstiges&gt;" in payload
    # And it round-trips, which is the point.
    assert find_one(_parse(payload), "listName").text == 'Kulanz & <Sonstiges> "x"'


def test_empty_viewfields_falls_back_to_properties_true() -> None:
    root = view_fields(None)
    assert root.get("Properties") == "TRUE"
    assert len(root) == 0


def test_element_helper_skips_none_attributes() -> None:
    el = element("FieldRef", {"Name": "ID", "Ascending": None})
    assert el.get("Name") == "ID"
    assert el.get("Ascending") is None


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def test_parse_response_returns_the_result_element() -> None:
    result = parse_response(fixture_bytes("lists_getlistcollection.xml"), "GetListCollection")
    assert local_name(result) == "GetListCollectionResult"
    assert len(find_all(result, "List")) == 5


def test_parse_response_reaches_across_the_four_namespaces_in_an_item_response() -> None:
    result = parse_response(fixture_bytes("lists_getlistitems_page1.xml"), "GetListItems")
    rows = find_all(result, "row")
    assert len(rows) == 2
    assert rows[0].get("ows_Title") == "Getriebeschaden"
    assert find_one(result, "data").get("ItemCount") == "2"


def test_parse_response_rejects_a_body_without_the_result_element() -> None:
    payload = b'<?xml version="1.0"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body/></soap:Envelope>'
    with pytest.raises(ValueError, match="no <GetListItemsResult>"):
        parse_response(payload, "GetListItems")


def test_parse_response_rejects_an_empty_body() -> None:
    with pytest.raises(ValueError):
        parse_response(b"", "GetListItems")


# --------------------------------------------------------------------------- #
# faults
# --------------------------------------------------------------------------- #


def test_soap_fault_raises_a_typed_exception() -> None:
    with pytest.raises(SharePointSoapFault) as excinfo:
        parse_response(fixture_bytes("soap_fault.xml"), "GetListItems", "http://sp/_vti_bin/Lists.asmx")

    fault = excinfo.value
    assert fault.operation == "GetListItems"
    assert fault.faultcode == "soap:Server"
    assert fault.faultstring == "Ausnahme wurde von einem Aufrufziel ausgelöst."
    assert fault.errorcode == "0x82000006"
    assert "Servicefälle" in (fault.errorstring or "")
    assert fault.endpoint == "http://sp/_vti_bin/Lists.asmx"
    assert "GetListItems failed" in str(fault)
    assert "0x82000006" in str(fault)


def test_fault_as_dict_is_manifest_ready() -> None:
    fault = parse_fault(_parse(fixture_bytes("soap_fault.xml")), "GetList")
    assert fault is not None
    assert fault.as_dict()["errorcode"] == "0x82000006"


def test_parse_fault_returns_none_for_a_healthy_response() -> None:
    root = _parse(fixture_bytes("lists_getlistcollection.xml"))
    assert parse_fault(root, "GetListCollection") is None


def test_fault_without_a_detail_block_still_parses() -> None:
    payload = (
        b'<?xml version="1.0"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        b"<soap:Body><soap:Fault><faultcode>soap:Client</faultcode>"
        b"<faultstring>Server was unable to process request.</faultstring>"
        b"</soap:Fault></soap:Body></soap:Envelope>"
    )
    with pytest.raises(SharePointSoapFault) as excinfo:
        parse_response(payload, "GetList")
    assert excinfo.value.errorcode is None
    assert excinfo.value.errorstring is None


# --------------------------------------------------------------------------- #
# unexpected bodies keep their evidence
# --------------------------------------------------------------------------- #


def test_unexpected_body_carries_the_bytes_that_explain_it() -> None:
    body = fixture_bytes("html_login_page.html")
    with pytest.raises(SoapResponseError) as excinfo:
        parse_response(body, "GetAllSubWebCollection", "http://sp/_vti_bin/Webs.asmx")

    exc = excinfo.value
    assert exc.operation == "GetAllSubWebCollection"
    assert exc.body == body
    assert exc.endpoint == "http://sp/_vti_bin/Webs.asmx"
    # The message must be actionable on its own, in a domain with no debugger.
    assert "no <GetAllSubWebCollectionResult>" in str(exc)
    assert "forms-authentication login page" in str(exc)
    assert "Anmelden" in str(exc)  # a snippet of the actual body


def test_unexpected_body_is_still_a_value_error() -> None:
    # Callers written against the old behaviour keep working.
    with pytest.raises(ValueError):
        parse_response(b"<html>nope</html>", "GetListItems")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"", "empty response body"),
        (b"   \n", "empty response body"),
        (b"<html><body>500 - Internal server error.</body></html>", "looks like an HTML page"),
        (
            b"<soap:Fault><faultstring>Server did not recognize the value of HTTP Header "
            b"SOAPAction: x.</faultstring></soap:Fault>",
            "does not implement this operation",
        ),
        (b"just some text", "not a SOAP envelope at all"),
        (b'<soap:Envelope xmlns:soap="x"><soap:Body/></soap:Envelope>', "without the expected result"),
    ],
)
def test_diagnose_body(body: bytes, expected: str) -> None:
    assert expected in diagnose_body(body)


def test_empty_response_is_diagnosed_not_just_reported() -> None:
    with pytest.raises(SoapResponseError, match="empty response body"):
        parse_response(b"", "GetListItems")


def test_body_snippet_is_truncated(tmp_path) -> None:
    body = b"<x>" + b"y" * 5000 + b"</x>"
    with pytest.raises(SoapResponseError) as excinfo:
        parse_response(body, "GetListItems")
    assert len(str(excinfo.value)) < 1000


def test_save_body_writes_the_whole_thing(tmp_path) -> None:
    body = fixture_bytes("html_login_page.html")
    with pytest.raises(SoapResponseError) as excinfo:
        parse_response(body, "GetAllSubWebCollection")

    target = excinfo.value.save_body(tmp_path / "landing" / "_last_bad_response.xml")

    assert target.read_bytes() == body  # full body, not the snippet


def test_list_view_threshold_fault_is_recognised_in_german() -> None:
    with pytest.raises(SharePointSoapFault) as excinfo:
        parse_response(fixture_bytes("soap_fault_threshold.xml"), "GetListItems")
    assert excinfo.value.is_list_view_threshold is True


def test_an_ordinary_fault_is_not_mistaken_for_throttling() -> None:
    with pytest.raises(SharePointSoapFault) as excinfo:
        parse_response(fixture_bytes("soap_fault.xml"), "GetListItems")
    assert excinfo.value.is_list_view_threshold is False


@pytest.mark.parametrize(
    "text",
    [
        "The attempted operation is prohibited because it exceeds the list view threshold",
        "Der Schwellenwert für die Listenansicht wurde überschritten",
    ],
)
def test_threshold_markers_cover_both_languages(text: str) -> None:
    fault = SharePointSoapFault("GetListItems", text)
    assert fault.is_list_view_threshold is True
