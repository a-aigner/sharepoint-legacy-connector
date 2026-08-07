"""``spconnect`` command line.

Flag precedence is CLI > env var > ``.env`` > default; the CLI layer implements
the first hop by passing overrides into :func:`spconnect.config.load_settings`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from . import __version__
from .config import Settings, get_logger, load_settings, setup_logging
from .console import StepReporter, format_bytes
from .crawl import LIST_VIEW_THRESHOLD, THRESHOLD_ADVICE, CrawlAborted, Crawler, RunReport
from .landing import LandingZone
from .models import ListSchema
from .permissions import probe_access
from .schema import graph_summary, render_dot, render_mermaid
from .services.lists import ListsService, is_system_list
from .services.odata import ODataService
from .services.sitedata import SiteDataService
from .services.usergroup import UserGroupService
from .services.webs import WebsService
from .soap import SoapResponseError
from .state import StateStore
from .transport import (
    AuthenticationError,
    AuthProbe,
    IntegratedAuthUnavailable,
    RedirectRefused,
    SharePointAccessDenied,
    Transport,
)

log = get_logger(__name__)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Read-only extraction connector for legacy on-premises SharePoint (WSS 2.0/3.0, MOSS 2007).",
)

SCOPE_HELP = "Comma-separated; overrides the matching SP_* setting."


class Context:
    """Resolved settings plus the shared transport, built lazily per command."""

    def __init__(self, settings: Settings, dry_run: bool) -> None:
        self.settings = settings
        self.dry_run = dry_run
        self._transport: Transport | None = None

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            self._transport = Transport(self.settings)
        return self._transport

    def crawler(self) -> Crawler:
        return Crawler(self.settings, self.transport)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None


def _ctx(ctx: typer.Context) -> Context:
    obj = ctx.obj
    if not isinstance(obj, Context):  # pragma: no cover - typer always populates this
        raise typer.BadParameter("CLI context was not initialised")
    return obj


def echo(message: str = "") -> None:
    typer.echo(message)


def _dump_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    env_file: Annotated[
        Path | None, typer.Option("--env-file", help="Path to the .env file to load.")
    ] = None,
    log_level: Annotated[
        str | None, typer.Option("--log-level", help="DEBUG | INFO | WARNING | ERROR.")
    ] = None,
    log_format: Annotated[str | None, typer.Option("--log-format", help="console | json.")] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Discovery only: print what would be crawled.")
    ] = False,
    base_url: Annotated[str | None, typer.Option("--base-url", help="Override SP_BASE_URL.")] = None,
    landing_dir: Annotated[
        Path | None, typer.Option("--landing-dir", help="Override SP_LANDING_DIR.")
    ] = None,
    include_webs: Annotated[str | None, typer.Option("--include-webs", help=SCOPE_HELP)] = None,
    exclude_webs: Annotated[str | None, typer.Option("--exclude-webs", help=SCOPE_HELP)] = None,
    include_lists: Annotated[str | None, typer.Option("--include-lists", help=SCOPE_HELP)] = None,
    exclude_lists: Annotated[str | None, typer.Option("--exclude-lists", help=SCOPE_HELP)] = None,
    include_hidden_lists: Annotated[
        bool | None, typer.Option("--include-hidden-lists/--no-include-hidden-lists")
    ] = None,
    include_document_libraries: Annotated[
        bool | None, typer.Option("--include-document-libraries/--no-include-document-libraries")
    ] = None,
    download_files: Annotated[bool | None, typer.Option("--download-files/--no-download-files")] = None,
    page_size: Annotated[int | None, typer.Option("--page-size", help="Override SP_PAGE_SIZE.")] = None,
    api_mode: Annotated[
        str | None,
        typer.Option("--api-mode", help="soap | odata — which API fetches list items."),
    ] = None,
    verbose: Annotated[
        int,
        typer.Option(
            "--verbose",
            "-v",
            count=True,
            help="-v: DEBUG logging (every HTTP request). -vv: also log request/response bodies.",
        ),
    ] = 0,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Suppress the step-by-step narration.")
    ] = False,
    version: Annotated[bool, typer.Option("--version", help="Print the version and exit.")] = False,
) -> None:
    if version:
        echo(f"spconnect {__version__}")
        raise typer.Exit(0)

    overrides: dict[str, Any] = {
        "log_level": log_level,
        "log_format": log_format,
        "base_url": base_url,
        "landing_dir": landing_dir,
        "include_webs": include_webs,
        "exclude_webs": exclude_webs,
        "include_lists": include_lists,
        "exclude_lists": exclude_lists,
        "include_hidden_lists": include_hidden_lists,
        "include_document_libraries": include_document_libraries,
        "download_files": download_files,
        "page_size": page_size,
        "api_mode": api_mode,
    }
    if verbose >= 1:
        overrides["log_level"] = "DEBUG"
    if verbose >= 2:
        overrides["log_bodies"] = True
    if quiet:
        overrides["show_steps"] = False
    settings = load_settings(env_file=env_file, overrides=overrides)
    if landing_dir is not None:
        # An overridden landing zone takes its state file with it.
        settings.state_file = Path(landing_dir) / "_state.json"
    setup_logging(settings.log_level, settings.log_format)
    ctx.obj = Context(settings, dry_run)


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #


def _domain_notes(context: Context, settings: Settings, auth: AuthProbe | None) -> list[str]:
    """What the server says its domain is, and whether SP_USERNAME agrees.

    Worth an extra unauthenticated round trip because "use DOMAIN\\user" is
    advice nobody can act on without knowing DOMAIN, and the people who hit this
    are usually the ones who cannot find out — the server, meanwhile, will say.

    Skipped unless the server offered a Windows scheme, so farms that never ask
    for one do not pay for a question with no answer.
    """
    offered = {s.lower() for s in (auth.schemes if auth else [])}
    if not offered & {"ntlm", "negotiate"}:
        return []

    target = context.transport.discover_ntlm_domain()
    if target is None:
        return []

    known = ", ".join(
        f"{label} {value}"
        for label, value in (
            ("NetBIOS domain:", target.netbios_domain),
            ("DNS domain:", target.dns_domain),
            ("host:", target.netbios_computer),
        )
        if value
    )
    notes = [f"server identifies as — {known}"]

    user = settings.username
    if target.username_hint and user and "\\" not in user and "@" not in user:
        notes.append(
            f"SP_USERNAME='{user}' has no domain. This server wants "
            f"{target.username_hint.replace('<user>', user)}"
        )
    return notes


def report_auth_failure(context: Context, settings: Settings, auth: AuthProbe | None, exc: Exception) -> None:
    """Everything we can establish about a 401, gathered here and now.

    Shared by every command that can hit one, because the evidence is the same
    evidence and there may be exactly one chance to collect it — a customer site
    visit is not the place to discover that only ``probe`` explains itself.
    """
    echo(f"\nAUTH FAILED: {exc}")
    echo("")

    if auth is None or auth.suggested_mode in (None, settings.auth_mode):
        for line in context.transport.diagnose_endpoint_auth(f"{settings.base_url}/_vti_bin/Webs.asmx"):
            echo(line)
    else:
        # Every request would 401 for the same trivial reason, and the
        # differential would read that as "no permissions anywhere". A
        # confident wrong answer is worse than no answer.
        echo(
            f"Skipping the differential check: SP_AUTH_MODE={settings.auth_mode} is not what "
            "this server offers, so every request fails the same way whatever the account "
            "can read. Fix the auth mode first, then re-run."
        )

    echo("")
    for line in _domain_notes(context, settings, auth):
        echo(line)
    echo("")

    if auth and auth.suggested_mode and auth.suggested_mode != settings.auth_mode:
        echo(f"Try SP_AUTH_MODE={auth.suggested_mode} — that is what this server offers.")
    elif settings.auth_mode == "ntlm" and "\\" not in settings.username:
        echo("NTLM usually wants DOMAIN\\username. Yours has no domain part.")
    if settings.auth_mode in ("ntlm", "basic"):
        echo("Consider SP_AUTH_MODE=integrated — it needs no password at all.")


def report_refused_redirect(exc: RedirectRefused) -> None:
    """One line, because there is exactly one thing to change and it is named."""
    if exc.suggested_base_url:
        echo(f"\nSet SP_BASE_URL={exc.suggested_base_url} and run `spconnect probe` again.")


@app.command()
def probe(ctx: typer.Context) -> None:
    """Auth check, server version, and one trivial call, narrated step by step.

    Exits nonzero on failure. Every step names what it is about to try before
    trying it, so a hang is attributable rather than mysterious.
    """
    context = _ctx(ctx)
    settings = context.settings
    steps = StepReporter(enabled=settings.show_steps and settings.log_format != "json", total=8)

    steps.heading(f"spconnect probe -> {settings.base_url}")
    if settings.auth_mode == "integrated":
        steps.info("auth mode   : integrated (current process identity — no password stored)")
    else:
        steps.info(f"auth mode   : {settings.auth_mode} (user: {settings.username or '<none>'})")
    steps.info(f"item source : SP_API_MODE={settings.api_mode}")
    steps.info(
        f"legacy TLS  : {'on' if settings.allow_legacy_tls else 'off'}, "
        f"verify SSL {'on' if settings.verify_ssl else 'off'}"
    )
    steps.info(f"rate limit  : {settings.requests_per_second}/s")
    if settings.log_bodies:
        steps.info(f"body trace  : {settings.resolved_trace_file} (mode 0600)")
    echo("")

    auth: AuthProbe | None = None
    version = None
    webs: list[Any] = []

    try:
        with steps.step("Reach the server (no credentials)") as st:
            auth = context.transport.probe_auth_schemes()
            if auth.error:
                raise ConnectionError(auth.error)
            st.detail(f"HTTP {auth.status}")
            if auth.redirect_to:
                st.note(f"-> {auth.redirect_to}")

        with steps.step("Determine authentication scheme") as st:
            # Before reporting an answer: a farm that redirects every request
            # has not answered, and the remaining steps would narrate progress
            # against a URL the farm does not serve.
            Transport.raise_for_base_url_redirect(auth)
            st.detail(auth.advice)
            if auth.suggested_mode and auth.suggested_mode != settings.auth_mode:
                st.note(
                    f"NOTE: SP_AUTH_MODE is '{settings.auth_mode}' but the server offers "
                    f"'{auth.suggested_mode}'. If the next step fails, try that."
                )
            for line in _domain_notes(context, settings, auth):
                st.note(line)

        identity = (
            "the current process identity"
            if settings.auth_mode == "integrated"
            else (settings.username or "<anonymous>")
        )
        with steps.step(f"Authenticate as {identity}") as st:
            version = context.transport.probe_version()
            if denied := context.transport.version_probe_denied_by:
                # A 200 from an access-denied page is still a 200. Letting this
                # step pass hands the next failure a wrong premise: the login is
                # fine, and everything after it fails for a reason that has
                # nothing to do with the credential.
                raise SharePointAccessDenied(
                    f"signed in, but SharePoint sent us to {denied}\n"
                    "  IIS accepted the credential and SharePoint then refused it access.\n"
                    "  That page answers HTTP 200, so this step used to pass and the first\n"
                    "  request with no friendly page to redirect to — the SOAP call — failed\n"
                    "  instead, several steps away from the cause.\n"
                    f"  This is a permissions problem: grant '{settings.username}' Read on\n"
                    "  the root web. The password is not the issue."
                )
            if settings.auth_mode == "anonymous":
                st.detail("reached (anonymous — no credential configured)")
            elif context.transport.version_probe_authenticated:
                st.detail("login successful")
            else:
                # A 2xx alone does not prove the credential works — it may just
                # mean this page needed no credential. Claiming otherwise sends
                # the next failure looking in the wrong place.
                st.detail("reached, but WITHOUT authenticating")
                st.note(
                    "NOTE: this page needed no credential, so the login is still unproven. "
                    "The first step that requires one will be the real test."
                )
            if settings.auth_mode == "basic":
                st.note("Basic transmits the password on every request. Prefer integrated or ntlm.")

        with steps.step("Read server build number") as st:
            st.detail(version.raw or "no version header")
            st.note(f"product: {version.product}")
            if not version.supports_change_tokens:
                st.note("This build predates change tokens; `sync` will do full crawls.")
            if version.has_list_view_threshold:
                st.note(f"{LIST_VIEW_THRESHOLD}-item list view threshold applies on this build.")

        with steps.step("Enumerate webs") as st:
            discovery = WebsService(context.transport, settings.base_url).discover_all_webs(
                prefer_recursive_call=version.supports_all_sub_web_collection
            )
            webs = discovery.webs
            st.detail(f"{len(webs)} readable via {discovery.method}")
            for web in webs[:10]:
                st.note(f"- {web.url}  {web.title}")
            if len(webs) > 10:
                st.note(f"… and {len(webs) - 10} more")
            for warning in discovery.warnings:
                st.note(f"NOTE: {warning}")
            st.note("If this count looks low, the credential lacks permissions somewhere.")

        with steps.step("List inventory on the first web") as st:
            lists = ListsService(context.transport, webs[0].url).get_list_collection()
            business = [li for li in lists if not is_system_list(li) and not li.hidden]
            st.detail(f"{len(lists)} lists, {len(business)} in scope")
            for info in sorted(business, key=lambda li: -li.item_count)[:10]:
                flags = " [unique-scopes]" if info.has_unique_scopes else ""
                st.note(f"{info.item_count:>8,}  {info.title} ({info.base_type_name}){flags}")

        with steps.step("SiteData liveness") as st:
            ok, detail = SiteDataService(context.transport, settings.base_url).reachable()
            st.detail("reachable" if ok else "unreachable")
            if detail:
                st.note(detail)

        with steps.step("ListData.svc (REST backend)") as st:
            rest_ok, rest_detail = ODataService(context.transport, settings.base_url).available()
            st.detail("available" if rest_ok else "unavailable")
            st.note(str(rest_detail))
            if rest_ok and webs:
                for line in _entity_set_mapping(context, webs[0].url):
                    st.note(line)
            elif settings.api_mode == "odata":
                st.note("SP_API_MODE=odata but REST is unavailable; every list would fall back to SOAP.")

    except IntegratedAuthUnavailable as exc:
        steps.done()
        echo(f"\n{exc}")
        raise typer.Exit(2) from exc
    except AuthenticationError as exc:
        steps.done()
        report_auth_failure(context, settings, auth, exc)
        raise typer.Exit(2) from exc
    except RedirectRefused as exc:
        # Not a farm problem and not worth a -vv suggestion: there is exactly
        # one thing to change, and it is already named in the message.
        steps.done()
        report_refused_redirect(exc)
        raise typer.Exit(2) from exc
    except SoapResponseError as exc:
        steps.done()
        saved = exc.save_body(settings.landing_dir / "_last_bad_response.xml")
        echo(f"\nFull response body written to {saved}")
        raise typer.Exit(1) from exc
    except Exception as exc:
        steps.done()
        echo("\nRe-run with -vv to see the full request and response.")
        raise typer.Exit(1) from exc
    finally:
        echo("")
        diagnostics = context.transport.side_channel_requests
        echo(
            f"{context.transport.request_count + diagnostics} HTTP requests"
            + (f" ({diagnostics} diagnostic)" if diagnostics else "")
            + f", {format_bytes(context.transport.bytes_received)} received"
        )
        trace = context.transport.trace
        if trace is not None and trace.entries:
            echo(f"{trace.entries} bodies captured to {trace.path} (mode 0600)")
        context.close()

    steps.done("PROBE OK — the connector can read this farm.")


# --------------------------------------------------------------------------- #
# probe-rest
# --------------------------------------------------------------------------- #


@app.command("probe-rest")
def probe_rest(ctx: typer.Context) -> None:
    """The same farm reached over REST instead of SOAP, as a second opinion.

    ``ListData.svc`` sits in the **same ``_vti_bin`` directory** as
    ``Webs.asmx`` and answers the **same credential**, but it is reached with a
    ``GET`` rather than a SOAP ``POST``. That one difference is the point: when
    ``probe`` fails at the first SOAP call, this separates a failure caused by
    the *request* from one caused by the *account* or the *location*.

    NTLM authenticates a connection, and IIS commonly drops that connection when
    it rejects a request carrying a body — which fails the handshake for POSTs
    while leaving bodyless GETs working. If REST succeeds here and SOAP does
    not, that is the shape of it, and no amount of password or permission work
    will help.
    """
    context = _ctx(ctx)
    settings = context.settings
    steps = StepReporter(enabled=settings.show_steps and settings.log_format != "json", total=5)

    steps.heading(f"spconnect probe-rest -> {settings.base_url}")
    steps.info(f"auth mode   : {settings.auth_mode} (user: {settings.username or '<none>'})")
    steps.info(f"endpoint    : {settings.base_url}/_vti_bin/ListData.svc")
    echo("")

    auth: AuthProbe | None = None
    rest_ok = False
    soap_ok = False
    soap_detail = ""

    try:
        with steps.step("Check the base URL") as st:
            auth = context.transport.probe_auth_schemes()
            if auth.error:
                raise ConnectionError(auth.error)
            Transport.raise_for_base_url_redirect(auth)
            st.detail(f"HTTP {auth.status} — {auth.advice}")

        service = ODataService(context.transport, settings.base_url)

        with steps.step("Reach ListData.svc (a GET, not a SOAP POST)") as st:
            ok, detail = service.available()
            rest_ok = ok
            st.detail(str(detail) if ok else "unavailable")
            if not ok:
                st.note(str(detail))

        with steps.step("Read one row over REST") as st:
            if not rest_ok:
                st.detail("skipped — ListData.svc is not answering")
            else:
                sets = service.entity_sets()
                if not sets:
                    st.detail("no entity sets exposed")
                else:
                    page = service.get_items(sets[0], top=1)
                    st.detail(f"{sets[0]}: {len(page.rows)} row(s)")
                    for name in sets[:10]:
                        st.note(f"- {name}")
                    if len(sets) > 10:
                        st.note(f"… and {len(sets) - 10} more")

        with steps.step("Same web and credential, via SOAP (a POST)") as st:
            # Deliberately not fatal, and not counted as a failed step: a SOAP
            # failure here is the measurement this command exists to take.
            try:
                webs = WebsService(context.transport, settings.base_url).get_all_sub_web_collection()
                soap_ok, soap_detail = True, f"{len(webs)} web(s)"
            except Exception as exc:
                soap_detail = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
            if soap_ok:
                st.detail(soap_detail)
            else:
                st.ok = False
                st.note(soap_detail)

        with steps.step("Verdict") as st:
            st.detail(f"REST {'ok' if rest_ok else 'failed'}, SOAP {'ok' if soap_ok else 'failed'}")
            for line in _rest_vs_soap_verdict(rest_ok, soap_ok):
                st.note(line)

    except AuthenticationError as exc:
        steps.done()
        report_auth_failure(context, settings, auth, exc)
        raise typer.Exit(2) from exc
    except RedirectRefused as exc:
        steps.done()
        report_refused_redirect(exc)
        raise typer.Exit(2) from exc
    except Exception as exc:
        steps.done()
        echo(f"\nFAILED: {type(exc).__name__}: {exc}")
        raise typer.Exit(1) from exc
    finally:
        echo("")
        echo(
            f"{context.transport.request_count + context.transport.side_channel_requests} "
            f"HTTP requests, {format_bytes(context.transport.bytes_received)} received"
        )
        context.close()

    steps.done("REST reachable." if rest_ok else "REST NOT reachable.")
    raise typer.Exit(0 if rest_ok else 1)


def _rest_vs_soap_verdict(rest_ok: bool, soap_ok: bool) -> list[str]:
    """What the two results together mean. The contrast is the whole point."""
    if rest_ok and soap_ok:
        return [
            "Both work. Nothing is isolated here — whatever failed elsewhere is not",
            "about REST versus SOAP.",
        ]
    if rest_ok and not soap_ok:
        return [
            "REST GET succeeds where the SOAP POST fails, on the same _vti_bin",
            "directory with the same credential. The account and the location are",
            "therefore fine, and the failure belongs to the POST itself — most",
            "likely NTLM's connection binding being broken when IIS rejects a",
            "request carrying a body.",
            "Ask the farm admin for Kerberos/Negotiate, or run as a domain identity",
            "with SP_AUTH_MODE=integrated.",
            "NOTE: this does NOT unblock a crawl. SP_API_MODE=odata changes only",
            "where *items* come from; web and list discovery still go over SOAP.",
        ]
    if not rest_ok and soap_ok:
        return [
            "SOAP works and REST does not — the OData feature is off, or this build",
            "predates ListData.svc. Keep SP_API_MODE=soap; nothing is wrong.",
        ]
    return [
        "Both fail the same way, so the failure is not about the request method.",
        "That points at the credential's access to this web, or at _vti_bin being",
        "restricted on this zone — not at SOAP. `spconnect permissions` next.",
    ]


# --------------------------------------------------------------------------- #
# permissions
# --------------------------------------------------------------------------- #


@app.command()
def permissions(
    ctx: typer.Context,
    probe_items: Annotated[
        bool,
        typer.Option(
            "--probe-items/--no-probe-items",
            help="Read one row per list to prove readability. Off = inventory only.",
        ),
    ] = True,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """What this credential can actually read, web by web and list by list.

    Answers "does the account have permissions" by trying, rather than by
    asking: enumerating a principal's permissions is itself privileged, and the
    read-only accounts this connector runs as usually cannot. Where the server
    *is* willing to name the account's groups and roles, that is reported too.

    Read-only. One list-collection call per web, one single-row read per list.
    """
    context = _ctx(ctx)
    settings = context.settings
    steps = StepReporter(enabled=settings.show_steps and not as_json, total=4)
    steps.heading(f"spconnect permissions -> {settings.base_url}")
    steps.info(f"identity    : {settings.username or 'the current process identity'}")
    echo("")

    auth: AuthProbe | None = None

    try:
        with steps.step("Check the base URL") as st:
            # One unauthenticated request, before anything that needs a login.
            # A farm that redirects every request cannot be asked about
            # permissions, and finding that out from a mangled SOAP call three
            # steps later names the wrong layer. It also gives the auth-failure
            # report below the scheme information it needs to be useful.
            auth = context.transport.probe_auth_schemes()
            if auth.error:
                raise ConnectionError(auth.error)
            Transport.raise_for_base_url_redirect(auth)
            st.detail(f"HTTP {auth.status} — {auth.advice}")

        with steps.step("Enumerate webs") as st:
            version = context.transport.probe_version()
            discovery = WebsService(context.transport, settings.base_url).discover_all_webs(
                prefer_recursive_call=version.supports_all_sub_web_collection
            )
            st.detail(f"{len(discovery.webs)} readable via {discovery.method}")

        with steps.step("Ask the server what it grants this account") as st:
            declared = UserGroupService(context.transport, settings.base_url).describe(settings.username)
            if declared.known:
                st.detail(f"groups: {', '.join(declared.groups) or 'none'}")
                st.note(f"permission levels: {', '.join(declared.roles) or 'none'}")
            else:
                # Expected, not alarming: reading someone's permissions is a
                # privilege in its own right, and the effective check below does
                # not need it.
                st.detail("not permitted to say")
                for reason in declared.unavailable:
                    st.note(reason)

        with steps.step("Read one row from every list in scope") as st:
            report = probe_access(context.transport, discovery.webs, probe_items=probe_items)
            st.detail(f"{report.readable_lists}/{report.total_lists} lists readable")
    except AuthenticationError as exc:
        steps.done()
        report_auth_failure(context, settings, auth, exc)
        echo("\nPermissions cannot be assessed until the login works.")
        raise typer.Exit(2) from exc
    except RedirectRefused as exc:
        steps.done()
        report_refused_redirect(exc)
        raise typer.Exit(2) from exc
    except Exception as exc:
        steps.done()
        echo(f"\nFAILED: {type(exc).__name__}: {exc}")
        raise typer.Exit(1) from exc
    finally:
        context.close()

    if as_json:
        echo(
            _dump_json(
                {
                    "base_url": settings.base_url,
                    "declared": {
                        "login": declared.login,
                        "groups": declared.groups,
                        "roles": declared.roles,
                        "unavailable": declared.unavailable,
                    },
                    "webs": [
                        {
                            "url": web.url,
                            "readable": web.readable,
                            "reason": web.reason,
                            "lists": [vars(entry) for entry in web.lists],
                        }
                        for web in report.webs
                    ],
                    "complete": report.complete,
                }
            )
        )
        return

    steps.done()
    echo("")
    for web in report.webs:
        if not web.readable:
            echo(f"DENIED  {web.url}\n        {web.reason}")
            continue
        echo(f"{web.url}  ({len(web.readable_lists)}/{len(web.lists)} lists readable)")
        for entry in sorted(web.lists, key=lambda li: (li.readable, -li.item_count)):
            mark = "  ok    " if entry.readable else "  DENIED"
            scopes = "  [unique-scopes]" if entry.has_unique_scopes else ""
            echo(f"{mark} {entry.item_count:>8,}  {entry.title}{scopes}")
            if entry.reason:
                echo(f"           {entry.reason}")
    echo("")

    if report.complete:
        echo("Everything discovered is readable by this credential.")
    else:
        echo(
            f"{len(report.denied_webs)} web(s) and "
            f"{report.total_lists - report.readable_lists} list(s) are not readable. "
            "A crawl would silently omit them — grant Read, or scope them out explicitly."
        )
    if scoped := report.unique_scope_lists:
        echo("")
        echo(
            f"{len(scoped)} list(s) have item-level permissions. Readable does not mean "
            "complete there: rows this account cannot see are simply absent, and no "
            "permission table above item level will show that."
        )


# --------------------------------------------------------------------------- #
# discover
# --------------------------------------------------------------------------- #


@app.command()
def discover(ctx: typer.Context) -> None:
    """Enumerate webs and lists into ``webs.json``. No items. Run this first."""
    context = _ctx(ctx)
    crawler = context.crawler()
    try:
        if context.dry_run:
            _print_dry_run(crawler.dry_run())
        else:
            webs, lists_by_web = crawler.discover()
            echo(f"{len(webs)} webs, {sum(len(v) for v in lists_by_web.values())} in-scope lists")
            for web_url, lists in lists_by_web.items():
                echo(f"\n{web_url}")
                for list_info in lists:
                    flags = []
                    if list_info.is_document_library:
                        flags.append("doclib")
                    if list_info.hidden:
                        flags.append("hidden")
                    if list_info.has_unique_scopes:
                        flags.append("unique-scopes")
                    suffix = f"  [{', '.join(flags)}]" if flags else ""
                    echo(f"  {list_info.item_count:>8,}  {list_info.title}{suffix}")
            echo(f"\nwebs.json -> {crawler.landing.webs_path}")
            crawler.write_manifest("discover")
        _print_unique_scope_warning(crawler.report)
    finally:
        context.close()


# --------------------------------------------------------------------------- #
# schema / graph
# --------------------------------------------------------------------------- #


@app.command()
def schema(ctx: typer.Context) -> None:
    """``GetList`` for every in-scope list; writes ``list.json`` files."""
    context = _ctx(ctx)
    crawler = context.crawler()
    try:
        _webs, lists_by_web = crawler.discover()
        schemas = crawler.fetch_schemas(lists_by_web)
        graph = crawler.build_graph(schemas)
        crawler.write_manifest("schema")

        echo(f"{len(schemas)} list schemas written under {crawler.landing.root}")
        for summary_key, value in graph_summary(graph).items():
            echo(f"  {summary_key:<20} {value}")
        echo(f"\nGraph: {crawler.landing.graph_mmd_path}")
        _print_unique_scope_warning(crawler.report)
    finally:
        context.close()


@app.command()
def graph(
    ctx: typer.Context,
    fmt: Annotated[str, typer.Option("--format", help="mermaid | json | dot")] = "mermaid",
    out: Annotated[Path | None, typer.Option("--out", help="Write to this file instead of stdout.")] = None,
) -> None:
    """Build and emit the lookup graph from the cached schemas."""
    context = _ctx(ctx)
    landing = LandingZone(context.settings.landing_dir)
    schemas: list[ListSchema] = list(landing.iter_list_schemas())
    if not schemas:
        echo(f"No cached schemas under {landing.root}. Run `spconnect schema` first.")
        raise typer.Exit(1)

    from .schema import build_lookup_graph

    lookup_graph = build_lookup_graph(schemas)
    landing.write_graph(lookup_graph)

    fmt = fmt.lower()
    if fmt == "json":
        rendered = _dump_json(lookup_graph.model_dump(mode="json"))
    elif fmt == "dot":
        rendered = render_dot(lookup_graph)
        landing.write_graph_dot(lookup_graph)
    elif fmt == "mermaid":
        rendered = render_mermaid(lookup_graph)
    else:
        raise typer.BadParameter("--format must be mermaid, json or dot")

    if out is not None:
        out.write_text(rendered, encoding="utf-8")
        echo(f"wrote {out}")
    else:
        echo(rendered)

    summary = graph_summary(lookup_graph)
    if summary["dangling_edges"]:
        echo(
            f"\n{summary['dangling_edges']} dangling lookup edge(s): the target list is out of "
            "scope or not readable by this credential.",
        )


# --------------------------------------------------------------------------- #
# crawl / sync
# --------------------------------------------------------------------------- #


@app.command()
def crawl(
    ctx: typer.Context,
    resume: Annotated[bool, typer.Option("--resume", help="Continue from the last checkpoint.")] = False,
) -> None:
    """Full extraction into the landing zone."""
    context = _ctx(ctx)
    crawler = context.crawler()
    try:
        if context.dry_run:
            _print_dry_run(crawler.dry_run())
            return
        report = crawler.crawl(resume=resume)
        crawler.write_manifest("crawl")
        _print_summary(report, crawler)
    except CrawlAborted as exc:
        echo(f"\nABORTED: {exc}")
        crawler.write_manifest("crawl")
        raise typer.Exit(2) from exc
    finally:
        context.close()


@app.command()
def sync(ctx: typer.Context) -> None:
    """Incremental update via change tokens, including deletes."""
    context = _ctx(ctx)
    crawler = context.crawler()
    try:
        if context.dry_run:
            _print_dry_run(crawler.dry_run())
            return
        report = crawler.sync()
        crawler.write_manifest("sync")
        _print_summary(report, crawler)
    except CrawlAborted as exc:
        echo(f"\nABORTED: {exc}")
        crawler.write_manifest("sync")
        raise typer.Exit(2) from exc
    finally:
        context.close()


# --------------------------------------------------------------------------- #
# verify-time / stats
# --------------------------------------------------------------------------- #


@app.command("verify-time")
def verify_time(
    ctx: typer.Context,
    list_name: Annotated[str, typer.Option("--list", help="List title or GUID.")],
    item: Annotated[int, typer.Option("--item", help="Item ID.")],
) -> None:
    """Print raw vs decoded datetimes for one item, so a human can check UTC."""
    context = _ctx(ctx)
    crawler = context.crawler()
    try:
        result = crawler.verify_time(list_name, item)
    except Exception as exc:
        echo(f"FAILED: {type(exc).__name__}: {exc}")
        raise typer.Exit(1) from exc
    finally:
        context.close()

    echo(f"List        : {result['list_title']}  ({result['list_guid']})")
    echo(f"Web         : {result['web_url']}")
    echo(f"Item        : {result['item_id']}")
    echo(f"Display form: {result['display_url']}")
    echo(f"Query option: {result['query_options']}")
    echo("")
    echo(f"{'FIELD':<28} {'RAW WIRE VALUE':<26} DECODED (UTC)")
    for row in result["fields"]:
        echo(f"{row['field'][:27]:<28} {str(row['raw_wire_value'])[:25]:<26} {row['decoded_utc']}")
    echo("")
    echo("Open the display form above and compare against what SharePoint shows.")
    echo("If they disagree, DateInUtc is not behaving as documented on this build.")


@app.command()
def stats(ctx: typer.Context) -> None:
    """Summarise the landing zone: lists, items, files, bytes, errors."""
    context = _ctx(ctx)
    landing = LandingZone(context.settings.landing_dir)
    if not landing.root.exists():
        echo(f"No landing zone at {landing.root}")
        raise typer.Exit(1)

    data = landing.stats()
    echo(f"Landing zone : {data['root']}")
    echo(f"Webs         : {data['webs']}")
    echo(f"Lists        : {data['lists']}")
    echo(f"Items        : {data['items']:,}")
    echo(f"Files        : {data['files']:,} ({data['file_bytes'] / 1024 / 1024:.1f} MB)")

    state_path = context.settings.state_file
    if state_path.exists():
        store = StateStore(state_path)
        by_status: dict[str, int] = {}
        for entry in store.state.lists.values():
            by_status[entry.status] = by_status.get(entry.status, 0) + 1
        echo(f"State        : {', '.join(f'{k}={v}' for k, v in sorted(by_status.items())) or 'empty'}")
        failed = [(g, e) for g, e in store.state.lists.items() if e.status == "failed"]
        for guid, entry in failed[:20]:
            echo(f"  FAILED {entry.list_title or guid}: {entry.error}")

    manifest = landing.read_manifest()
    if manifest is not None:
        echo(f"Last command : {manifest.command} at {manifest.finished_at}")
        echo(f"Server       : {manifest.server_version.get('product', 'unknown')}")
        if manifest.errors:
            echo(f"Errors       : {len(manifest.errors)}")
            for error in manifest.errors[:20]:
                echo(f"  [{error.scope}] {error.list_title or error.web_url}: {error.message[:160]}")
        if manifest.lists_with_unique_scopes:
            echo("")
            _print_unique_scope_list(manifest.lists_with_unique_scopes)

    if data["per_list"]:
        echo("")
        echo(f"{'ITEMS':>10}  {'FILES':>7}  LIST")
        for row in sorted(data["per_list"], key=lambda r: -int(r["items"]))[:40]:
            echo(f"{row['items']:>10,}  {row['files']:>7,}  {row['list_title'] or row['path']}")


# --------------------------------------------------------------------------- #
# output helpers
# --------------------------------------------------------------------------- #


def _entity_set_mapping(context: Context, web_url: str) -> list[str]:
    """How list titles survive ListData.svc's entity-set sanitiser.

    Names are derived from titles — spaces dropped, words capitalised, non-ASCII
    mangled — so on a German farm this is the thing to eyeball before trusting
    the REST backend.
    """
    try:
        odata = ODataService(context.transport, web_url)
        lists = [
            li
            for li in ListsService(context.transport, web_url).get_list_collection()
            if not is_system_list(li) and not li.hidden
        ]
    except Exception as exc:
        return [f"(could not compare list titles: {exc})"]

    lines: list[str] = []
    unmapped: list[str] = []
    for info in lists[:15]:
        entity = odata.entity_set_for(info.title)
        lines.append(f"{'->' if entity else '!!'} {info.title!r} -> {entity or 'NO MATCH'}")
        if not entity:
            unmapped.append(info.title)
    if unmapped:
        lines.append(f"{len(unmapped)} list(s) have no REST entity set; those fall back to SOAP.")
    return lines


def _print_dry_run(plan: dict[str, Any]) -> None:
    echo("DRY RUN — nothing was fetched beyond discovery.\n")
    echo(f"{'ITEMS':>10}  {'PAGES':>6}  {'REQ':>8}  LIST")
    for row in sorted(plan["rows"], key=lambda r: -int(r["items"])):
        flag = "  [unique-scopes]" if row["has_unique_scopes"] else ""
        echo(
            f"{row['items']:>10,}  {row['pages']:>6,}  {row['estimated_requests']:>8,}  "
            f"{row['list_title']}{flag}"
        )
    echo("")
    echo(f"Webs               : {plan['webs']}")
    echo(f"Lists              : {plan['lists']}")
    echo(f"Items              : {plan['items']:,}")
    echo(f"Estimated requests : {plan['estimated_requests']:,}")
    echo(f"Estimated wall time: ~{plan['estimated_minutes']:,} min at the configured rate limit")


def _print_unique_scope_list(entries: list[str]) -> None:
    echo("WARNING: item-level permissions detected on these lists:")
    for entry in entries[:20]:
        echo(f"  - {entry}")
    if len(entries) > 20:
        echo(f"  … and {len(entries) - 20} more")
    echo(
        "This connector crawls as a single identity, so per-item security is "
        "flattened. The downstream vector DB will not preserve it."
    )


def _print_threshold_warning(report: RunReport) -> None:
    if report.throttled_lists:
        echo("")
        echo("ERROR: these lists were throttled by the SharePoint 2010 list view threshold:")
        for entry in report.throttled_lists[:20]:
            echo(f"  - {entry}")
        echo(THRESHOLD_ADVICE.format(threshold=LIST_VIEW_THRESHOLD))
    elif report.large_lists:
        echo("")
        echo(f"NOTE: {len(report.large_lists)} list(s) hold more than {LIST_VIEW_THRESHOLD:,} items:")
        for entry in report.large_lists[:10]:
            echo(f"  - {entry}")
        if len(report.large_lists) > 10:
            echo(f"  … and {len(report.large_lists) - 10} more")
        echo("They crawled fine — ID-based paging seeks the index rather than scanning.")


def _print_unique_scope_warning(report: RunReport) -> None:
    if report.unique_scope_lists:
        echo("")
        _print_unique_scope_list(report.unique_scope_lists)


def _print_summary(report: RunReport, crawler: Crawler) -> None:
    echo("")
    echo("─" * 72)
    echo("SUMMARY")
    echo(f"  webs discovered    : {report.webs_discovered}")
    echo(f"  lists in scope     : {report.lists_in_scope}")
    echo(f"  lists succeeded    : {report.lists_succeeded}")
    echo(f"  lists failed       : {report.lists_failed}")
    echo(f"  lists skipped      : {report.lists_skipped} (already complete)")
    echo(f"  items written      : {report.items_written:,}")
    echo(f"  items deleted      : {report.items_deleted:,}")
    echo(f"  files downloaded   : {report.files_downloaded:,} ({report.file_bytes / 1024 / 1024:.1f} MB)")
    echo(f"  files skipped      : {report.files_skipped:,}")
    for reason, count in sorted(report.skip_reasons.items()):
        echo(f"      {reason:<16} {count}")
    echo(f"  decoder warnings   : {report.decoder_warnings:,}")
    if report.large_lists:
        echo(f"  over 5000 items    : {len(report.large_lists)} list(s)")
    if report.throttled_lists:
        echo(f"  throttled by 2010  : {len(report.throttled_lists)} list(s)")
    if report.odata_fallbacks:
        echo(f"  REST -> SOAP       : {len(report.odata_fallbacks)} list(s) fell back")
    echo(f"  dangling lookups   : {report.dangling_edges}")
    echo(f"  item source        : {crawler.settings.api_mode}")
    echo(f"  landing zone       : {crawler.landing.root}")
    echo(f"  manifest           : {crawler.landing.manifest_path}")

    if report.errors:
        echo("")
        echo(f"ERRORS ({len(report.errors)}) — recorded in _manifest.json:")
        for error in report.errors[:20]:
            echo(f"  [{error.scope}] {error.list_title or error.web_url}: {error.message[:160]}")
        if len(report.errors) > 20:
            echo(f"  … and {len(report.errors) - 20} more")

    if report.warnings:
        echo("")
        echo("WARNINGS:")
        for warning in report.warnings[:20]:
            echo(f"  - {warning}")

    _print_threshold_warning(report)
    _print_unique_scope_warning(report)


def run() -> None:  # pragma: no cover - console-script shim
    try:
        app()
    except KeyboardInterrupt:
        echo(
            "\nInterrupted. State was checkpointed after the last completed page; "
            "re-run with `spconnect crawl --resume`."
        )
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    run()
