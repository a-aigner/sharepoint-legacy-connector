"""``Webs.asmx`` — site discovery.

Two operations, because one of them does not exist everywhere:

* ``GetAllSubWebCollection`` returns the whole subtree in one call, but arrived
  with **WSS 3.0**. On WSS 2.0 / SPS 2003 it is simply absent.
* ``GetWebCollection`` returns only the *immediate* children of the web it is
  called on, and has existed since WSS 2.0.

:meth:`WebsService.discover_all_webs` prefers the first and walks the tree with
the second when it is unavailable, so the crawler does not have to care.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import get_logger
from ..models import WebRef, normalise_url
from ..soap import SharePointSoapFault, SoapClient, SoapResponseError, find_all
from ..transport import AuthenticationError, TransportError

log = get_logger(__name__)

#: Depth guard for the WSS 2.0 walk. Real farms of this era are nowhere near it.
DEFAULT_MAX_DEPTH = 32


@dataclass
class WebDiscovery:
    """The web inventory plus how it was obtained."""

    webs: list[WebRef] = field(default_factory=list)
    method: str = "GetAllSubWebCollection"
    depth_reached: int = 0
    warnings: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """False when some subtree could not be read and may be missing webs."""
        return not self.unreadable


class WebsService:
    """Wraps ``Webs.asmx`` for one web."""

    def __init__(self, transport: Any, web_url: str) -> None:
        self.transport = transport
        self.client = SoapClient(transport, web_url, "Webs")
        self.web_url = normalise_url(web_url)

    # ---- raw operations ----

    def _parse_webs(self, result: Any) -> list[WebRef]:
        webs: list[WebRef] = []
        for el in find_all(result, "Web"):
            url = el.get("Url") or el.get("url") or ""
            if not url:
                continue
            webs.append(WebRef(title=el.get("Title") or "", url=url))
        return webs

    def get_all_sub_web_collection(self) -> list[WebRef]:
        """Every web beneath this one that the credential can read, recursively.

        Includes the called web itself, so the caller gets a complete inventory
        from a single call. The count is worth reporting prominently: a low
        number means the credential is missing permissions somewhere, not that
        the farm is small.

        WSS 3.0 and later only — see :meth:`discover_all_webs`.
        """
        result = self.client.call("GetAllSubWebCollection")

        seen: dict[str, WebRef] = {}
        root = WebRef(title="", url=self.web_url)
        seen[root.url] = root
        for ref in self._parse_webs(result):
            existing = seen.get(ref.url)
            if existing is None:
                seen[ref.url] = ref
            elif not existing.title and ref.title:
                existing.title = ref.title

        webs = sorted(seen.values(), key=lambda w: w.url)
        log.info("webs.discovered", count=len(webs), root=self.web_url)
        return webs

    def get_web_collection(self) -> list[WebRef]:
        """The *immediate* child webs of this web. Available since WSS 2.0."""
        return self._parse_webs(self.client.call("GetWebCollection"))

    # ---- orchestration ----

    def discover_all_webs(
        self,
        *,
        prefer_recursive_call: bool = True,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> WebDiscovery:
        """Full inventory, whichever operation this build actually supports.

        ``prefer_recursive_call=False`` skips straight to the WSS 2.0 walk; the
        crawler sets that from the version probe rather than guessing.
        """
        if prefer_recursive_call:
            try:
                webs = self.get_all_sub_web_collection()
                return WebDiscovery(webs=webs, method="GetAllSubWebCollection")
            except AuthenticationError:
                raise
            except (SharePointSoapFault, SoapResponseError) as exc:
                reason = (
                    f"GetAllSubWebCollection unavailable ({type(exc).__name__}); "
                    "falling back to a recursive GetWebCollection walk"
                )
                log.warning("webs.fallback", detail=reason, error=str(exc).splitlines()[0])
                discovery = self._walk(max_depth)
                discovery.warnings.insert(0, reason)
                return discovery
        else:
            log.info(
                "webs.walk",
                detail="server predates GetAllSubWebCollection; walking with GetWebCollection",
            )
            discovery = self._walk(max_depth)
            discovery.warnings.insert(
                0, "server build predates GetAllSubWebCollection; used a GetWebCollection walk"
            )
            return discovery

    def _walk(self, max_depth: int) -> WebDiscovery:
        """Breadth-first walk using immediate-children calls only."""
        discovery = WebDiscovery(method="GetWebCollection")
        root = WebRef(title="", url=self.web_url)
        seen: dict[str, WebRef] = {root.url: root}
        queue: list[tuple[str, int]] = [(root.url, 0)]

        while queue:
            url, depth = queue.pop(0)
            if depth >= max_depth:
                warning = f"stopped at depth {max_depth} under {url}; deeper webs were not visited"
                log.warning("webs.depth_limit", url=url, max_depth=max_depth)
                discovery.warnings.append(warning)
                continue

            try:
                children = WebsService(self.transport, url).get_web_collection()
            except AuthenticationError:
                raise
            except (SharePointSoapFault, SoapResponseError, TransportError) as exc:
                if depth == 0:
                    # Failing on the *root* is not a partial result, it is no
                    # result. Reporting "1 web" here would dress up a total
                    # failure as near-success.
                    log.error("webs.root_unreadable", url=url, error=str(exc).splitlines()[0])
                    raise
                # One unreadable subweb, though, must not cost us the rest of the farm.
                log.warning("webs.subweb_unreadable", url=url, error=str(exc).splitlines()[0])
                discovery.unreadable.append(url)
                continue

            discovery.depth_reached = max(discovery.depth_reached, depth)
            for child in children:
                existing = seen.get(child.url)
                if existing is not None:
                    if not existing.title and child.title:
                        existing.title = child.title
                    continue
                seen[child.url] = child
                queue.append((child.url, depth + 1))

        discovery.webs = sorted(seen.values(), key=lambda w: w.url)
        log.info(
            "webs.discovered",
            count=len(discovery.webs),
            root=self.web_url,
            method=discovery.method,
            depth=discovery.depth_reached,
            unreadable=len(discovery.unreadable),
        )
        return discovery
