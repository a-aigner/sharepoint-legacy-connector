"""Thin wrappers over the classic ASMX services.

Exactly three services are used: ``Webs.asmx`` for site discovery,
``Lists.asmx`` for everything about lists and items, and ``SiteData.asmx``
purely as an optional liveness/version probe. Nothing else is implemented on
purpose — see the spec's non-goals.
"""

from .lists import ListsService
from .sitedata import SiteDataService
from .webs import WebsService

__all__ = ["ListsService", "SiteDataService", "WebsService"]
