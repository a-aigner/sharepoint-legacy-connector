"""``Webs.asmx`` — site discovery."""

from __future__ import annotations

from typing import Any

from ..config import get_logger
from ..models import WebRef, normalise_url
from ..soap import SoapClient, find_all

log = get_logger(__name__)


class WebsService:
    """Wraps ``Webs.asmx`` for one web."""

    def __init__(self, transport: Any, web_url: str) -> None:
        self.client = SoapClient(transport, web_url, "Webs")
        self.web_url = normalise_url(web_url)

    def get_all_sub_web_collection(self) -> list[WebRef]:
        """Every web beneath this one that the credential can read, recursively.

        Includes the called web itself, so the caller gets a complete inventory
        from a single call. The count is worth reporting prominently: a low
        number means the credential is missing permissions somewhere, not that
        the farm is small.
        """
        result = self.client.call("GetAllSubWebCollection")

        seen: dict[str, WebRef] = {}
        root = WebRef(title="", url=self.web_url)
        seen[root.url] = root

        for el in find_all(result, "Web"):
            url = el.get("Url") or el.get("url") or ""
            if not url:
                continue
            ref = WebRef(title=el.get("Title") or "", url=url)
            existing = seen.get(ref.url)
            if existing is None:
                seen[ref.url] = ref
            elif not existing.title and ref.title:
                existing.title = ref.title

        webs = sorted(seen.values(), key=lambda w: w.url)
        log.info("webs.discovered", count=len(webs), root=self.web_url)
        return webs
