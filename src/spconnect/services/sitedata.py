"""``SiteData.asmx`` — optional liveness probe only.

Used by ``spconnect probe`` as a second opinion when the version header is
missing. No data is extracted through this service, and nothing else in the
crawler depends on it being available.
"""

from __future__ import annotations

from typing import Any

from ..config import get_logger
from ..models import normalise_url
from ..soap import SharePointSoapFault, SoapClient

log = get_logger(__name__)


class SiteDataService:
    def __init__(self, transport: Any, web_url: str) -> None:
        self.client = SoapClient(transport, web_url, "SiteData")
        self.web_url = normalise_url(web_url)

    def reachable(self) -> tuple[bool, str | None]:
        """``GetSiteAndWeb`` on the web's own URL: cheap, read-only, harmless.

        Returns ``(ok, detail)``. A SOAP fault still proves the endpoint is
        alive and authenticated, so it counts as reachable.
        """
        try:
            self.client.call("GetSiteAndWeb", {"strUrl": self.web_url})
        except SharePointSoapFault as exc:
            log.debug("sitedata.fault", detail=str(exc))
            return True, f"responded with a SOAP fault: {exc}"
        except Exception as exc:
            log.debug("sitedata.unreachable", detail=str(exc))
            return False, str(exc)
        return True, None
