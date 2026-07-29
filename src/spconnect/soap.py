"""Hand-rolled SOAP envelopes for the classic ``_vti_bin/*.asmx`` services.

Hand-rolled rather than ``zeep`` on purpose: three of the parameters we care
about (``query``, ``viewFields``, ``queryOptions``) take *XML fragments as
element content*, and generated clients tend to escape them into strings. Here
they are built as real ``lxml`` elements, so escaping is structurally
impossible to get wrong.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

from lxml import etree

from .config import get_logger

SOAP_ENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"
SP_SOAP_NS = "http://schemas.microsoft.com/sharepoint/soap/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
XSD_NS = "http://www.w3.org/2001/XMLSchema"

ENVELOPE_NSMAP = {"xsi": XSI_NS, "xsd": XSD_NS, "soap": SOAP_ENV_NS}

ParamValue = str | int | None | etree._Element | Sequence["etree._Element"]

log = get_logger(__name__)


#: Substrings identifying an ``SPQueryThrottledException`` surfaced as a fault.
#: German is included deliberately — this farm's UI language is German.
LIST_VIEW_THRESHOLD_MARKERS = (
    "list view threshold",
    "exceeds the list view",
    "listenansichtsschwellenwert",
    "schwellenwert für die listenansicht",
    "0x80070024",
)


class SharePointSoapFault(Exception):
    """A ``<soap:Fault>`` body. An application error, not a transport error."""

    def __init__(
        self,
        operation: str,
        faultstring: str,
        *,
        faultcode: str | None = None,
        errorcode: str | None = None,
        errorstring: str | None = None,
        endpoint: str | None = None,
    ) -> None:
        detail = errorstring or faultstring
        super().__init__(f"{operation} failed: {detail}" + (f" [{errorcode}]" if errorcode else ""))
        self.operation = operation
        self.faultstring = faultstring
        self.faultcode = faultcode
        self.errorcode = errorcode
        self.errorstring = errorstring
        self.endpoint = endpoint

    @property
    def is_list_view_threshold(self) -> bool:
        """SharePoint 2010+ throttling, in either language this farm might use."""
        haystack = " ".join(
            part for part in (self.faultstring, self.errorstring, self.errorcode) if part
        ).lower()
        return any(marker in haystack for marker in LIST_VIEW_THRESHOLD_MARKERS)

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "faultcode": self.faultcode,
            "faultstring": self.faultstring,
            "errorcode": self.errorcode,
            "errorstring": self.errorstring,
            "endpoint": self.endpoint,
        }


# --------------------------------------------------------------------------- #
# XML helpers
# --------------------------------------------------------------------------- #


def sp_tag(name: str) -> str:
    """Qualify ``name`` into the SharePoint SOAP namespace."""
    return f"{{{SP_SOAP_NS}}}{name}"


def element(
    name: str,
    attrib: Mapping[str, Any] | None = None,
    text: str | None = None,
    children: Iterable[etree._Element] = (),
) -> etree._Element:
    """Build a CAML/SOAP element in the SharePoint namespace.

    Values are set through ``lxml``, so any ``&``, ``<`` or quote in a
    user-supplied value is escaped by the serialiser rather than by us.
    """
    el = etree.Element(sp_tag(name))
    for key, value in (attrib or {}).items():
        if value is not None:
            el.set(key, str(value))
    if text is not None:
        el.text = text
    for child in children:
        el.append(child)
    return el


def local_name(el: etree._Element) -> str:
    return etree.QName(el).localname


def text_content(el: etree._Element) -> str:
    """All descendant text of ``el`` as one string. Mixed content is common here."""
    return "".join(str(part) for part in el.itertext())


def find_all(root: etree._Element, name: str) -> list[etree._Element]:
    """Namespace-agnostic descendant search. These responses mix four namespaces."""
    return cast(list[etree._Element], root.xpath(".//*[local-name()=$n]", n=name))


def find_one(root: etree._Element, name: str) -> etree._Element | None:
    found = find_all(root, name)
    return found[0] if found else None


def to_bytes(el: etree._Element) -> bytes:
    return etree.tostring(el, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Envelope build / parse
# --------------------------------------------------------------------------- #


def build_envelope(operation: str, params: Mapping[str, ParamValue] | None = None) -> bytes:
    """Serialise a full SOAP 1.1 request envelope for ``operation``."""
    envelope = etree.Element(f"{{{SOAP_ENV_NS}}}Envelope", nsmap=ENVELOPE_NSMAP)
    body = etree.SubElement(envelope, f"{{{SOAP_ENV_NS}}}Body")
    op = etree.SubElement(body, sp_tag(operation))

    for name, value in (params or {}).items():
        param = etree.SubElement(op, sp_tag(name))
        if value is None:
            continue
        if isinstance(value, etree._Element):
            param.append(value)
        elif isinstance(value, str | int):
            param.text = str(value)
        else:
            for child in value:
                param.append(child)

    return etree.tostring(envelope, xml_declaration=True, encoding="utf-8")


def soap_action(operation: str) -> str:
    """The unquoted SOAPAction value. The transport adds the required quotes."""
    return SP_SOAP_NS + operation


def parse_fault(
    root: etree._Element, operation: str, endpoint: str | None = None
) -> SharePointSoapFault | None:
    """Return a typed fault if the document carries one, else ``None``."""
    fault = find_one(root, "Fault")
    if fault is None:
        return None

    def text_of(name: str) -> str | None:
        el = find_one(fault, name)
        if el is None:
            return None
        return text_content(el).strip() or None

    return SharePointSoapFault(
        operation=operation,
        faultstring=text_of("faultstring") or "unknown SOAP fault",
        faultcode=text_of("faultcode"),
        errorcode=text_of("errorcode"),
        errorstring=text_of("errorstring"),
        endpoint=endpoint,
    )


SNIPPET_BYTES = 400


def diagnose_body(body: bytes) -> str:
    """Guess why a body is not the SOAP we asked for, in operator-readable terms."""
    if not body.strip():
        return "empty response body — a proxy or WAF may be stripping it"
    head = body[:8192].lower()
    if b"did not recognize the value of http header soapaction" in head:
        return "the server does not implement this operation on this build"
    if b"login.aspx" in head or (b"<form" in head and b'name="__viewstate"' in head):
        return "looks like a forms-authentication login page, not SOAP — FBA is not supported"
    if head.lstrip()[:6].startswith(b"<html") or b"<html" in head[:512]:
        return "looks like an HTML page, not SOAP — an IIS error page, a proxy, or a login form"
    if b"<soap:envelope" not in head and b"envelope" not in head:
        return "not a SOAP envelope at all"
    return "a SOAP envelope, but without the expected result element"


class SoapResponseError(ValueError):
    """The server answered, but not with the SOAP we asked for.

    Carries the body. Discarding the bytes that would explain the failure is
    exactly the wrong thing to do on a farm you cannot casually re-query.
    """

    def __init__(
        self,
        operation: str,
        message: str,
        *,
        body: bytes = b"",
        endpoint: str | None = None,
    ) -> None:
        self.operation = operation
        self.body = body
        self.endpoint = endpoint
        self.diagnosis = diagnose_body(body)
        snippet = body[:SNIPPET_BYTES].decode("utf-8", "replace").strip()
        detail = f"{operation}: {message} ({self.diagnosis})"
        if snippet:
            detail += f"\n  first {min(len(body), SNIPPET_BYTES)} bytes: {snippet}"
        super().__init__(detail)

    def save_body(self, path: Any) -> Any:
        """Write the full body to a ``0600`` file for inspection.

        Captured error bodies routinely contain session cookies and whatever the
        proxy felt like echoing, so this is not a world-readable artifact.
        """
        from pathlib import Path

        from .trace import write_private_bytes

        return write_private_bytes(Path(path), self.body)


def parse_response(payload: bytes, operation: str, endpoint: str | None = None) -> etree._Element:
    """Parse a response body and return the ``{operation}Result`` element.

    Raises :class:`SharePointSoapFault` on a fault body and
    :class:`SoapResponseError` (a ``ValueError``) on anything else unexpected.
    """
    parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False)
    try:
        root = etree.fromstring(payload, parser=parser)
    except etree.XMLSyntaxError as exc:  # pragma: no cover - recover=True rarely raises
        raise SoapResponseError(
            operation, "response was not parseable XML", body=payload, endpoint=endpoint
        ) from exc
    if root is None:
        raise SoapResponseError(operation, "empty response body", body=payload, endpoint=endpoint)

    fault = parse_fault(root, operation, endpoint)
    if fault is not None:
        raise fault

    result = find_one(root, f"{operation}Result")
    if result is None:
        raise SoapResponseError(
            operation,
            f"no <{operation}Result> element in response",
            body=payload,
            endpoint=endpoint,
        )
    return result


class SoapClient:
    """One service endpoint on one web. Endpoints are per-web, not per-farm."""

    def __init__(self, transport: Any, web_url: str, service: str) -> None:
        self.transport = transport
        self.web_url = web_url.rstrip("/")
        self.service = service

    @property
    def endpoint(self) -> str:
        return f"{self.web_url}/_vti_bin/{self.service}.asmx"

    def call(self, operation: str, params: Mapping[str, ParamValue] | None = None) -> etree._Element:
        body = build_envelope(operation, params)
        log.debug(
            "soap.call",
            operation=operation,
            endpoint=self.endpoint,
            params=sorted((params or {}).keys()),
            envelope_bytes=len(body),
        )
        started = time.monotonic()
        payload = self.transport.post_soap(self.endpoint, body, soap_action(operation))
        result = parse_response(payload, operation, self.endpoint)
        log.debug(
            "soap.ok",
            operation=operation,
            duration=round(time.monotonic() - started, 3),
            response_bytes=len(payload),
        )
        return result
