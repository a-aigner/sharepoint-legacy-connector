"""What this credential can actually read, established by trying.

There are two ways to answer "what are this user's permissions", and they are
not equally useful:

* **Declared** — ask ``UserGroup.asmx`` which groups and permission levels the
  account holds. Precise when it works, but enumerating a principal's
  permissions is itself privileged, so the read-only accounts this connector
  runs as frequently cannot. See :mod:`spconnect.services.usergroup`.
* **Effective** — attempt the reads the crawler would attempt and record what
  came back. Needs no privilege beyond what the crawl needs anyway, and answers
  the question the operator actually has, which is not "what role is this
  account in" but "will the extraction get the data".

This module does the second. When the two disagree, this one is right: a role
assignment that is overridden by a broken inheritance somewhere below is a
permission the account does not effectively have, and the crawl will find that
out whatever the role table said.

Read-only throughout, and bounded: one list-collection call per web and one
single-item read per list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import get_logger
from .models import WebRef
from .services.lists import ListsService, is_system_list
from .soap import SharePointSoapFault, SoapResponseError
from .transport import AuthenticationError, TransportError

log = get_logger(__name__)

#: One row is enough to prove readability, and costs the farm almost nothing.
PROBE_ROW_LIMIT = 1


def _reason(exc: Exception) -> str:
    return str(exc).splitlines()[0]


@dataclass
class ListAccess:
    """Whether one list yielded data, and why not when it did not."""

    title: str
    guid: str
    item_count: int = 0
    has_unique_scopes: bool = False
    readable: bool = False
    reason: str | None = None


@dataclass
class WebAccess:
    """Whether one web yielded a list inventory, and what those lists gave."""

    url: str
    title: str = ""
    readable: bool = False
    reason: str | None = None
    lists: list[ListAccess] = field(default_factory=list)

    @property
    def readable_lists(self) -> list[ListAccess]:
        return [entry for entry in self.lists if entry.readable]

    @property
    def denied_lists(self) -> list[ListAccess]:
        return [entry for entry in self.lists if not entry.readable]


@dataclass
class AccessReport:
    """The whole picture, per web and per list."""

    webs: list[WebAccess] = field(default_factory=list)

    @property
    def readable_webs(self) -> list[WebAccess]:
        return [web for web in self.webs if web.readable]

    @property
    def denied_webs(self) -> list[WebAccess]:
        return [web for web in self.webs if not web.readable]

    @property
    def total_lists(self) -> int:
        return sum(len(web.lists) for web in self.webs)

    @property
    def readable_lists(self) -> int:
        return sum(len(web.readable_lists) for web in self.webs)

    @property
    def unique_scope_lists(self) -> list[ListAccess]:
        """Lists with broken inheritance — where "readable" can vary per item.

        A list this account can open may still hide rows from it, and no
        permission table above item level will show that. Worth naming, because
        it is the difference between "the crawl is complete" and "the crawl is
        complete as far as this credential can see".
        """
        return [entry for web in self.webs for entry in web.lists if entry.has_unique_scopes]

    @property
    def complete(self) -> bool:
        return not self.denied_webs and self.readable_lists == self.total_lists


def probe_access(
    transport: Any,
    webs: list[WebRef],
    *,
    include_system_lists: bool = False,
    probe_items: bool = True,
) -> AccessReport:
    """Try what the crawler would try, and record the outcome.

    ``AuthenticationError`` is allowed to propagate: a session-wide auth failure
    is not a permission finding, and recording it as "nothing is readable" would
    dress a broken login up as a permissions problem.
    """
    report = AccessReport()

    for web in webs:
        entry = WebAccess(url=web.url, title=web.title)
        report.webs.append(entry)
        service = ListsService(transport, web.url)

        try:
            inventory = service.get_list_collection()
        except AuthenticationError:
            raise
        except (SharePointSoapFault, SoapResponseError, TransportError) as exc:
            entry.reason = _reason(exc)
            log.info("permissions.web_denied", url=web.url, reason=entry.reason)
            continue

        entry.readable = True
        for info in inventory:
            if not include_system_lists and (is_system_list(info) or info.hidden):
                continue
            access = ListAccess(
                title=info.title,
                guid=info.guid,
                item_count=info.item_count,
                has_unique_scopes=info.has_unique_scopes,
            )
            entry.lists.append(access)

            if not probe_items:
                access.readable = True
                continue
            try:
                service.get_list_items(info.guid, row_limit=PROBE_ROW_LIMIT)
            except AuthenticationError:
                raise
            except (SharePointSoapFault, SoapResponseError, TransportError) as exc:
                access.reason = _reason(exc)
                log.info("permissions.list_denied", list=info.title, reason=access.reason)
                continue
            access.readable = True

    log.info(
        "permissions.report",
        webs=len(report.webs),
        readable_webs=len(report.readable_webs),
        lists=report.total_lists,
        readable_lists=report.readable_lists,
    )
    return report
