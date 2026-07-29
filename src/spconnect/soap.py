"""Hand-rolled SOAP envelopes for the classic ``_vti_bin/*.asmx`` services.

Hand-rolled rather than ``zeep`` on purpose: three of the parameters we care
about (``query``, ``viewFields``, ``queryOptions``) take *XML fragments as
element content*, and generated clients tend to escape them into strings. Here
they are built as real ``lxml`` elements, so escaping is structurally
impossible to get wrong.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

from lxml import etree

SOAP_ENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"
SP_SOAP_NS = "http://schemas.microsoft.com/sharepoint/soap/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
XSD_NS = "http://www.w3.org/2001/XMLSchema"

ENVELOPE_NSMAP = {"xsi": XSI_NS, "xsd": XSD_NS, "soap": SOAP_ENV_NS}

ParamValue = str | int | None | etree._Element | Sequence["etree._Element"]


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


def parse_response(payload: bytes, operation: str, endpoint: str | None = None) -> etree._Element:
    """Parse a response body and return the ``{operation}Result`` element.

    Raises :class:`SharePointSoapFault` on a fault body, and ``ValueError`` when
    the response is not the expected shape.
    """
    parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False)
    try:
        root = etree.fromstring(payload, parser=parser)
    except etree.XMLSyntaxError as exc:  # pragma: no cover - recover=True rarely raises
        raise ValueError(f"{operation}: response was not parseable XML: {exc}") from exc
    if root is None:
        raise ValueError(f"{operation}: empty response body")

    fault = parse_fault(root, operation, endpoint)
    if fault is not None:
        raise fault

    result = find_one(root, f"{operation}Result")
    if result is None:
        raise ValueError(f"{operation}: no <{operation}Result> element in response")
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
        payload = self.transport.post_soap(self.endpoint, body, soap_action(operation))
        return parse_response(payload, operation, self.endpoint)
