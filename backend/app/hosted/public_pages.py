"""The public pages readable without JavaScript (ADR 0045 step 8): ``/``, ``/terms``,
``/privacy``, ``/refunds`` and ``/contact`` as real HTML.

A visitor, a search engine or a payment company's checker that does not run JavaScript would
otherwise get the app's empty page. The proxy sends these five addresses here when the request
carries no session cookie (deploy/Caddyfile); a signed-in reader gets the app's own page from the
proxy, as before, and never reaches this.

The words are not written here. ``npm run build`` draws each page with the app's own components
into the built index.html, with a placeholder (``@@name@@``) for each value only the server knows
(frontend/src/public/prerender.tsx), and the backend image carries the result. Each request fills
it in from exactly what ``GET /api/site`` answers (``site.site_answer``, the same prices kept),
every value escaped for HTML, and the browser's app then takes over and draws the same page.

The prices are written as the app writes them: by the currency table the build read from the
app's own formatting, and only if this module's figures agree with every plan the build checked
against the app's own (``checks``); otherwise the page shows the plan without figures, as it does
when Paddle cannot be read. Where the operator is not set, the pages say Mothli is not open here,
as the app does. Where the file is missing or not as expected, the answer is 503 and the proxy
serves the app's own page instead.
"""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from app.hosted.auth.api import SESSION_COOKIE
from app.hosted.site import site_answer

router = APIRouter(tags=["hosted-public-pages"])

_LOG = logging.getLogger(__name__)

# /app/backend/public/public-pages.json in the image (deploy/Dockerfile).
PAGES_FILE = Path(__file__).resolve().parents[2] / "public" / "public-pages.json"
PATHS = {"/": "front", "/terms": "terms", "/privacy": "privacy", "/refunds": "refunds",
         "/contact": "contact"}
VERSIONS = {"front": {"o0p0", "o0p1", "o0p2", "o1p0", "o1p1", "o1p2"},
            "terms": {"o0", "o1"}, "privacy": {"o0", "o1w0", "o1w1"},
            "refunds": {"o0", "o1"}, "contact": {"o0", "o1"}}
_SLOT = re.compile(r"@@([a-z_]+)@@")
_ROOT = '<div id="root"></div>'
# The HTML may change whenever a price or the operator's line does: always asked again.
_HEADERS = {"Cache-Control": "no-cache"}


class PublicPages:
    """The build's pages, its currency table and its checks, read once."""

    def __init__(self, data: dict) -> None:
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("not a public pages file this server reads")
        shell, pages, money = data.get("shell"), data.get("pages"), data.get("money")
        if not isinstance(shell, str) or shell.count(_ROOT) != 1:
            raise ValueError("the app's page is missing")
        if not isinstance(pages, dict) or not isinstance(money, dict):
            raise ValueError("the pages or the currency table are missing")
        for name, versions in VERSIONS.items():
            found = pages.get(name)
            if (not isinstance(found, dict) or set(found) != versions
                    or not all(isinstance(page, str) for page in found.values())):
                raise ValueError(f"the {name} page is not as expected")
        self.shell = shell
        self.pages = pages
        self.money = {currency: entry for currency, entry in money.items()
                      if isinstance(currency, str) and isinstance(entry, list) and len(entry) == 3
                      and isinstance(entry[0], str) and isinstance(entry[1], str)
                      and isinstance(entry[2], int) and 0 <= entry[2] <= 4}
        checks = data.get("checks")
        self.priced = bool(checks) and isinstance(checks, list) and all(
            isinstance(check, dict) and self.shown(check.get("plan"), checked=False)
            == check.get("shown") for check in checks)
        if not self.priced:
            _LOG.warning("public pages show no prices: the server's figures disagree with the app's")

    @classmethod
    def load(cls, path: Path = PAGES_FILE) -> PublicPages | None:
        try:
            return cls(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            _LOG.warning("public pages not written out: the built file is missing or not as "
                         "expected")
            return None

    def amount(self, minor: int, currency: str) -> str | None:
        """An amount in the currency's smallest unit, as the app writes it ("$12.99")."""
        entry = self.money.get(currency)
        if entry is None:
            return None
        before, after, digits = entry
        whole, part = divmod(minor, 10 ** digits)
        written = f"{whole:,}" + (f".{part:0{digits}d}" if digits else "")
        return f"{before}{written}{after}"

    def shown(self, plan: object, *, checked: bool = True) -> dict | None:
        """The front page's figures for the catalog's two prices, worked out as the app works
        them out (``planShown``), or None."""
        if (checked and not self.priced) or not isinstance(plan, dict):
            return None
        try:
            monthly, yearly = int(plan["monthly"]), int(plan["yearly"])
            currency = plan["currency"]
        except (KeyError, TypeError, ValueError):
            return None
        if monthly <= 0 or yearly <= 0 or not isinstance(currency, str):
            return None
        # Math.round(yearly / 12) and Math.floor(...) of the app, in whole numbers.
        per_month = (yearly + 6) // 12
        saving = ((monthly * 12 - yearly) * 100) // (monthly * 12)
        figures = {"monthly": self.amount(monthly, currency),
                   "yearlyTotal": self.amount(yearly, currency),
                   "yearlyPerMonth": self.amount(per_month, currency),
                   "saving": saving if saving >= 1 else None}
        if None in (figures["monthly"], figures["yearlyTotal"], figures["yearlyPerMonth"]):
            return None
        return figures

    def page(self, name: str, site: dict) -> str | None:
        """The page filled in from ``GET /api/site``'s answer, or None if a placeholder has no
        value (never written out with a hole)."""
        operator, writer = site.get("operator"), site.get("writer")
        named = "o1" if operator else "o0"
        values: dict[str, str] = {}
        if operator:
            values.update(legal_name=operator["legal_name"], country=operator["country"],
                          contact_email=operator["contact_email"])
        if name == "front":
            shown = self.shown(site.get("plan"))
            key = named + ("p0" if shown is None else "p2" if shown["saving"] is None else "p1")
            if shown is not None:
                values.update(monthly=shown["monthly"], yearly_total=shown["yearlyTotal"],
                              yearly_per_month=shown["yearlyPerMonth"])
                if shown["saving"] is not None:
                    values["saving"] = str(shown["saving"])
        elif name == "privacy" and operator:
            # The app names the company only as the read gives it: text of at most 100.
            told = isinstance(writer, str) and writer.strip() != "" and len(writer) <= 100
            key = "o1w1" if told else "o1w0"
            if told:
                values["writer"] = writer
        else:
            key = named
        missing = False

        def fill(found: re.Match) -> str:
            nonlocal missing
            value = values.get(found.group(1))
            if value is None:
                missing = True
                return ""
            return html.escape(value, quote=True)

        written = _SLOT.sub(fill, self.pages[name][key])
        return None if missing else written


def _pages(request: Request) -> PublicPages | None:
    state = request.app.state
    if not hasattr(state, "public_pages"):
        state.public_pages = PublicPages.load()
    return state.public_pages


async def _answer(request: Request) -> Response:
    pages = _pages(request)
    if pages is None:
        return PlainTextResponse("Mothli's page is not ready here.", status_code=503)
    name = PATHS[request.url.path]
    # Signed in, or a payment link's checkout (`/?_ptxn=`): the app's own page, as the proxy
    # serves it, with nothing about anyone in it.
    if name == "front" and (SESSION_COOKIE in request.cookies or "_ptxn" in request.query_params):
        return HTMLResponse(pages.shell, headers=_HEADERS)
    written = pages.page(name, await site_answer(request))
    if written is None:
        return HTMLResponse(pages.shell, headers=_HEADERS)
    return HTMLResponse(written, headers=_HEADERS)


for _path in PATHS:
    router.add_api_route(_path, _answer, methods=["GET", "HEAD"], include_in_schema=False)
