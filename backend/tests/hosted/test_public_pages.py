"""The public pages readable without JavaScript (ADR 0045 step 8): ``/``, ``/terms``,
``/privacy``, ``/refunds`` and ``/contact`` fetched the way a program fetches them (no script
run), through the hosted app, with Paddle's catalog stood in.

The pages are the ones ``npm run build`` writes from the app's own components
(frontend/prerendered/public-pages.json). Locally these tests are skipped until the frontend is
built; CI builds it first and sets ``MOTHLI_PUBLIC_PAGES_REQUIRED`` so they never skip there.
frontend/src/public/prerender.test.tsx proves each page is exactly what the app draws."""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.hosted.auth.api import SESSION_COOKIE
from app.hosted.paddle_api import PaddleApi
from app.hosted.public_pages import PATHS, VERSIONS, PublicPages
from tests.hosted.test_billing import KEY, OPERATOR, Recorder
from tests.hosted.test_companion_stream_settings import CHAT, _SignedIn
from tests.hosted.test_paddle_messages_postgres import MONTHLY, YEARLY_NEW
from tests.hosted.test_platform_pays import _Tenant
from tests.hosted.test_public_site import GOOD, KEYED, PADDLE, catalog, entity

ROOT = Path(__file__).resolve().parents[3]
BUILT = ROOT / "frontend" / "prerendered" / "public-pages.json"
NAV = ('<a href="/terms">Terms</a>', '<a href="/privacy">Privacy</a>',
       '<a href="/refunds">Refunds</a>', '<a href="/contact">Contact</a>')
NOT_OPEN = "Mothli isn’t open for sign-ups here yet."


@pytest.fixture(scope="module")
def built() -> dict:
    if not BUILT.exists():
        if os.environ.get("MOTHLI_PUBLIC_PAGES_REQUIRED"):
            pytest.fail("frontend/prerendered/public-pages.json is missing: run npm run build")
        pytest.skip("the frontend is not built (npm run build)")
    return json.loads(BUILT.read_text(encoding="utf-8"))


def fetch(tmp_path, data: dict | None, paths, answer=None, cookies=None, **values):
    """Each path fetched signed out (or with ``cookies``) through the hosted app; Paddle stood in
    by ``answer``. Returns the responses by path and the stand-in's recorder."""
    from app.main import create_app
    from tests.hosted.test_companion_stream_settings import hosted_settings

    recorder = Recorder(answer or catalog(GOOD))
    app = create_app(hosted_settings(tmp_path, **values), auth_repository=_SignedIn(),
                     tenant_repository=_Tenant(), oidc_client=object())
    with TestClient(app, base_url="https://reader.example", cookies=cookies) as client:
        app.state.paddle_api = PaddleApi(KEY, "sandbox", transport=httpx.MockTransport(recorder))
        app.state.public_pages = PublicPages(data) if data is not None else None
        found = {path: client.get(path) for path in paths}
    return found, recorder


def text(page: str) -> str:
    """What a person reads: the root's text, tags taken out."""
    root = page.split('<div id="root">', 1)[1]
    inline = re.sub(r"</?(?:a|strong|span)\b[^>]*>", "", root)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inline)).strip()


def test_the_front_page_says_what_mothli_is_what_it_costs_and_who_sells_it(tmp_path,
                                                                          built) -> None:
    """The break it catches: a program fetching ``/`` finding the empty app page (no product, no
    price, no policy link, no name), a price not the catalog's or not as the app writes it, or
    the server asking Paddle for anything more than for ``GET /api/site``."""
    found, recorder = fetch(tmp_path, built, ["/", "/api/site"], **KEYED)
    page = found["/"]
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["cache-control"] == "no-cache"
    assert page.headers["content-security-policy"].startswith("default-src 'self'")
    body = page.text
    for words in (
            "Read with a friend in the margin.",
            "Mothli reads along with you and only knows it as far as you’ve read, so it can "
            "never spoil what happens next.",
            "I only know the pages you’ve read, so no spoilers. Promise.",
            "Reading here is free, always: your books, your place, your highlights and the free library.",
            "Mothli knows the book only as far as you’ve read it",
            # The prices as the app writes them, from 12.99 a month and 79.99 a year.
            "$6.67 a month, billed $79.99 a year",
            "48% less than paying monthly.",
            "$12.99 a month",
            "Payments are handled by Paddle, who sells Mothli on our behalf.",
            "Mothli is run by Example Reading Ltd.",
            '<link rel="canonical" href="/" />',
            '<meta property="og:title" content="Mothli'):
        assert words in body, words
    for link in NAV:
        assert link in body, link
    assert "@@" not in body and NOT_OPEN not in body
    # ADR 0046: no free weeks, anywhere on the front page.
    assert "free weeks" not in body and "no card needed" not in body
    # The same prices as the read the app makes, from one read of Paddle.
    assert found["/api/site"].json()["plan"] == {"currency": "USD", "monthly": "1299",
                                                 "yearly": "7999"}
    assert [r.url.path for r in recorder.requests] == [f"/prices/{MONTHLY}",
                                                       f"/prices/{YEARLY_NEW}"]


def test_each_policy_page_is_there_in_full_with_who_runs_mothli(tmp_path, built) -> None:
    """The break it catches: the terms, privacy, refunds or contact page empty without
    JavaScript, the legal name, country or address missing from them, a section left out, or a
    page without the links to the others."""
    found, _recorder = fetch(tmp_path, built, ["/terms", "/privacy", "/refunds", "/contact"],
                             **KEYED)
    for path, page in found.items():
        assert page.status_code == 200, path
        assert page.headers["cache-control"] == "no-cache", path
        for link in NAV:
            assert link in page.text, (path, link)
        assert f'<link rel="canonical" href="{path}" />' in page.text
        assert "@@" not in page.text and NOT_OPEN not in page.text, path
    terms = text(found["/terms"].text)
    assert "Mothli is run by Example Reading Ltd, based in Ireland" in terms
    assert "These terms are governed by the law of Ireland." in terms
    assert '<a href="mailto:hello@reader.example">hello@reader.example</a>' in found["/terms"].text
    assert "<title>Terms of use · Mothli</title>" in found["/terms"].text
    for heading in ("Who runs Mothli", "What Mothli is", "Your account", "Your books",
                    "Free reading, and the plan", "Paying for the plan", "Cancelling",
                    "Using Mothli fairly", "Mothli’s answers come from AI", "Changes",
                    "Ending your account", "Our responsibility", "The law", "Contact"):
        assert f"<h2>{heading}</h2>" in found["/terms"].text, heading
    assert "Paddle sells the plan to you on our behalf." in terms
    assert "Nothing in these terms takes away rights you have by law as a consumer." in terms
    privacy = text(found["/privacy"].text)
    assert "Example Reading Ltd, based in Ireland, runs Mothli and is responsible" in privacy
    assert "Mothli’s chat isn’t open on this server yet" in privacy
    for heading in ("Who is responsible", "What we keep, and why", "Who else handles it",
                    "What other readers can see", "How long we keep it",
                    "Your copy, and your choices", "Cookies, and what stays on your device",
                    "Children"):
        assert f"<h2>{heading}</h2>" in found["/privacy"].text, heading
    refunds = text(found["/refunds"].text)
    assert "Refunds are decided by Paddle" in refunds
    # ADR 0046: the free weeks are gone, and "Try it first" with them.
    assert "Try it first" not in refunds and "free weeks" not in refunds + terms + privacy
    assert 'href="https://www.paddle.com/legal/refund-policy"' in found["/refunds"].text
    contact = text(found["/contact"].text)
    assert "Write to us at hello@reader.example." in contact
    assert "Example Reading Ltd, based in Ireland." in contact


def test_the_privacy_page_names_the_ai_company_the_server_uses(tmp_path, built) -> None:
    found, _recorder = fetch(tmp_path, built, ["/privacy"], **OPERATOR, **CHAT)
    page = text(found["/privacy"].text)
    assert "OpenAI, whose AI model writes Mothli’s replies and notes" in page
    assert "isn’t open on this server" not in page


def test_without_prices_or_operator_the_pages_say_nothing_untrue(tmp_path, built) -> None:
    """The break it catches: a figure where none was read, a name or terms published with a
    detail missing, a placeholder or an error shown to a visitor."""
    for values in ({}, PADDLE):
        found, recorder = fetch(tmp_path, built, list(PATHS), **values)
        assert recorder.requests == []
        front = found["/"].text
        assert found["/"].status_code == 200
        assert "Read with a friend in the margin." in front
        assert "You’ll see the prices when you choose." in front
        assert NOT_OPEN in front
        assert "a month, billed" not in front and "$" not in text(front)
        assert "Mothli is run by" not in front
        for path in ("/terms", "/privacy", "/refunds", "/contact"):
            page = found[path].text
            assert found[path].status_code == 200, path
            assert NOT_OPEN in page, path
            assert "<h2>" not in page and "mailto:" not in page, path
            for link in NAV:
                assert link in page, (path, link)
        for page in found.values():
            assert "@@" not in page.text
    # The operator set and Paddle not: the name, and the plan without figures.
    found, _recorder = fetch(tmp_path, built, ["/"], **OPERATOR)
    assert "Mothli is run by Example Reading Ltd." in found["/"].text
    assert "You’ll see the prices when you choose." in found["/"].text


def test_paddle_failing_gives_the_plan_without_figures(tmp_path, built) -> None:
    found, _recorder = fetch(tmp_path, built, ["/"], lambda r: httpx.Response(503), **KEYED)
    assert found["/"].status_code == 200
    assert "You’ll see the prices when you choose." in found["/"].text
    assert "a month, billed" not in found["/"].text


def test_a_plan_without_a_saving_says_none(tmp_path, built) -> None:
    prices = {MONTHLY: entity(MONTHLY, "1000", "month"),
              YEARLY_NEW: entity(YEARLY_NEW, "12000", "year")}
    found, _recorder = fetch(tmp_path, built, ["/"], catalog(prices), **KEYED)
    body = found["/"].text
    assert "$10.00 a month, billed $120.00 a year" in body
    assert "less than paying monthly" not in body


def test_the_operators_details_are_escaped(tmp_path, built) -> None:
    """The break it catches: a value from the settings written into the page as markup."""
    named = {**KEYED, "operator_legal_name": 'Smith & Jones "Books" Ltd'}
    found, _recorder = fetch(tmp_path, built, ["/", "/terms"], **named)
    for page in found.values():
        assert "Smith &amp; Jones &quot;Books&quot; Ltd" in page.text
        assert 'Smith & Jones "Books"' not in page.text


def test_a_signed_in_reader_or_a_payment_link_gets_the_app_itself(tmp_path, built) -> None:
    """The break it catches: a signed-in reader's ``/`` showing the visitor's page (the proxy
    sends them to the app's own page; this is the backend's own guard), or a payment link's
    checkout drawn under the front page."""
    found, recorder = fetch(tmp_path, built, ["/"], cookies={SESSION_COOKIE: "x" * 43}, **KEYED)
    assert found["/"].status_code == 200
    assert found["/"].text == built["shell"]
    assert '<div id="root"></div>' in found["/"].text
    assert recorder.requests == []
    found, _recorder = fetch(tmp_path, built, ["/?_ptxn=txn_" + "a" * 26], **KEYED)
    assert next(iter(found.values())).text == built["shell"]


def test_without_the_built_pages_the_answer_is_503_for_the_proxy(tmp_path) -> None:
    """The break it catches: a missing or broken file served as a broken page instead of the
    proxy's fallback (the app's own page)."""
    found, _recorder = fetch(tmp_path, None, list(PATHS), **KEYED)
    for page in found.values():
        assert page.status_code == 503
    assert PublicPages.load(tmp_path / "missing.json") is None
    (tmp_path / "broken.json").write_text('{"version": 1}', encoding="utf-8")
    assert PublicPages.load(tmp_path / "broken.json") is None


def test_no_prices_unless_this_servers_figures_agree_with_the_apps(tmp_path, built) -> None:
    """The break it catches: the server's sums drifting from the app's (planShown) and a price
    shown that the app would not show."""
    pages = PublicPages(built)
    assert pages.priced and len(built["checks"]) >= 5
    for check in built["checks"]:
        assert pages.shown(check["plan"]) == check["shown"]
    drifted = copy.deepcopy(built)
    drifted["checks"][0]["shown"]["yearlyPerMonth"] = "$6.66"
    assert not PublicPages(drifted).priced
    found, _recorder = fetch(tmp_path, drifted, ["/"], **KEYED)
    assert "You’ll see the prices when you choose." in found["/"].text
    assert "$12.99" not in found["/"].text


def test_a_head_request_is_answered(tmp_path, built) -> None:
    from app.main import create_app
    from tests.hosted.test_companion_stream_settings import hosted_settings

    app = create_app(hosted_settings(tmp_path), auth_repository=_SignedIn(),
                     tenant_repository=_Tenant(), oidc_client=object())
    with TestClient(app, base_url="https://reader.example") as client:
        app.state.public_pages = PublicPages(built)
        assert client.head("/terms").status_code == 200


def test_the_build_writes_every_version_the_server_reads(built) -> None:
    assert {name: set(versions) for name, versions in built["pages"].items()} == VERSIONS


def test_the_proxy_sends_only_requests_without_a_session_here() -> None:
    """The break it catches: a signed-in reader's page made slower or written by the backend; a
    public page not sent here; no fallback to the app's own page when the backend fails; the
    proxy's health check depending on the backend."""
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    readable = caddyfile.split("\t@readable {\n", 1)[1].split("\n\t}\n", 1)[0]
    listed = re.findall(r"^\t\tpath (.+)$", readable, flags=re.M)
    assert [set(paths.split()) for paths in listed] == [set(PATHS)]
    assert f"not header_regexp Cookie (^|;\\s*){SESSION_COOKIE}=" in readable
    proxied = caddyfile.split("\thandle @readable {\n", 1)[1].split("\n\t}\n", 1)[0]
    assert "reverse_proxy web:8000" in proxied
    assert "handle_response @failed" in proxied and "rewrite * /index.html" in proxied
    errors = caddyfile.split("\thandle_errors 502 503 504 {\n", 1)[1].split("\n\t}\n", 1)[0]
    assert set(re.findall(r"@unanswered path (.+)$", errors, flags=re.M)[0].split()) == set(PATHS)
    compose = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    assert '"http://127.0.0.1:8080/index.html"' in compose
    dockerfile = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    assert ("COPY --from=frontend /build/prerendered/public-pages.json "
            "./public/public-pages.json") in dockerfile
