"""``UserGroup.asmx`` — what SharePoint says an account has been granted.

This is the *declared* half of the permissions question. It is deliberately
best-effort: enumerating another principal's groups and roles is itself a
privileged operation, and the read-only accounts this connector is given are
routinely not allowed to do it. Every call therefore degrades to a reason
string rather than an exception, because "we could not ask" is a legitimate
answer that must not be confused with "the account has nothing".

The *effective* half — what the credential can actually read — lives in
:mod:`spconnect.permissions`, needs no privilege at all, and is the one to
trust when the two disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import get_logger
from ..models import normalise_url
from ..soap import SharePointSoapFault, SoapClient, find_all
from ..transport import AuthenticationError, TransportError

log = get_logger(__name__)


def login_variants(username: str) -> list[str]:
    """The forms this account's login could take, most likely first.

    A SharePoint 2010 web application may run in classic mode, where the login
    name is what Windows says (``DOMAIN\\user``), or in claims mode, where it is
    encoded (``i:0#.w|DOMAIN\\user``). The connector cannot tell which from the
    outside, and asking with the wrong form yields "user not found" rather than
    an error that explains itself — so try both.
    """
    user = username.strip()
    if not user:
        return []
    variants = [user]
    if not user.startswith("i:0"):
        variants.append(f"i:0#.w|{user}")
    return variants


@dataclass
class UserPermissions:
    """What the server was willing to say about one account."""

    login: str = ""
    groups: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    #: Why a lookup produced nothing, when it produced nothing.
    unavailable: list[str] = field(default_factory=list)

    @property
    def known(self) -> bool:
        return bool(self.groups or self.roles)


class UserGroupService:
    """Wraps ``UserGroup.asmx`` for one web."""

    def __init__(self, transport: Any, web_url: str) -> None:
        self.transport = transport
        self.client = SoapClient(transport, web_url, "UserGroup")
        self.web_url = normalise_url(web_url)

    def _names(self, operation: str, login: str, tag: str) -> tuple[list[str], str | None]:
        """``(names, reason_it_failed)`` — never raises except on auth failure.

        An ``AuthenticationError`` is re-raised deliberately: that is not this
        service being restricted, it is the whole session being unusable, and
        swallowing it here would report an authentication problem as an empty
        permission set.
        """
        try:
            result = self.client.call(operation, {"userLoginName": login})
        except AuthenticationError:
            raise
        except SharePointSoapFault as exc:
            return [], f"{operation}: {exc.errorstring or exc.faultstring}"
        except (TransportError, ValueError) as exc:
            return [], f"{operation}: {str(exc).splitlines()[0]}"

        names = [name for element in find_all(result, tag) if (name := (element.get("Name") or "").strip())]
        return names, None

    def describe(self, username: str) -> UserPermissions:
        """Groups and permission levels for ``username``, as far as we may look."""
        variants = login_variants(username)
        if not variants:
            return UserPermissions(unavailable=["no SP_USERNAME configured to ask about"])

        reasons: list[str] = []
        for login in variants:
            groups, group_error = self._names("GetGroupCollectionFromUser", login, "Group")
            roles, role_error = self._names("GetRoleCollectionFromUser", login, "Role")
            if groups or roles:
                log.info("permissions.declared", login=login, groups=groups, roles=roles)
                return UserPermissions(login=login, groups=groups, roles=roles)
            reasons.extend(reason for reason in (group_error, role_error) if reason)

        # Deduplicated: asking about two login forms produces the same
        # complaint twice, and printing it twice implies two problems.
        seen: dict[str, None] = {}
        for reason in reasons:
            seen.setdefault(reason, None)
        return UserPermissions(login=variants[0], unavailable=list(seen))
