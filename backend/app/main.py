"""The FastAPI app factory + lifespan (ADR 0007 D-A11). ``create_app(settings)`` builds the service
around the per-process singletons; the lifespan owns their startup/shutdown (Store.close serializes
against in-flight sessions; the D-A7 fail-loud predicate fires during startup, refusing a stub deploy
unless ALLOW_STUB is explicitly set)."""
import asyncio
import hashlib
import logging
import json
import tempfile
from time import monotonic, perf_counter
from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles

from app import deps
from app.api import books, classics, health, history, ingest, profile, reading, views
from app.config import Settings
from app.ingest.worker import IngestWorker
from app.hosted.auth import account as account_api
from app.hosted.auth import api as auth_api
from app.hosted.auth.oidc import OIDCClient
from app.hosted.auth.repository import PostgresAuthRepository
from app.hosted.companion import routes as companion_routes
from app.hosted.companion import set_connection_limit
from app.hosted.companion.source import SourceParseCache
from app.hosted.companion.spend import set_spend_limits
from app.hosted.credentials import build_credential_cipher
from app.hosted.migrations import discover_migrations
from app.hosted.provider_settings import ProviderValidator, allowed_origins
from app.hosted.runtime import HostedRuntimeRegistry
from app.hosted.storage import build_object_storage
from app.hosted.tenant import api as tenant_api
from app.hosted.tenant.repository import PostgresTenantRepository
from app.lifecycle.archive import DataDirLock
from app.lifecycle_cache import RecapRegistry
from app.llm.client import ProviderUnavailable
from app.llm.factory import build_platform_clients
from app.llm.versioning import ModelMigrationRequired

_MULTIPART_OVERHEAD_BYTES = 64 * 1024
_CREDENTIAL_BODY_BYTES = 32 * 1024
_LOG = logging.getLogger(__name__)


def _platform_ai_configured(settings: Settings) -> bool:
    """Whether hosted startup builds the platform's model clients: once a companion feature that
    calls a model is on, or once any platform AI setting is given (so a half-set one is refused
    at startup rather than at a reader's first question)."""
    return bool(settings.reader_conversation_jev or settings.reader_story_notes
                or settings.reader_recollections or settings.jev_enabled
                or settings.openai_api_key or settings.anthropic_api_key
                or settings.typesafe_api_key is not None)


# Hosted readiness (ADR 0041 part 9). The runtime roles may read no table outside their
# allow-lists, the migration table included, so the deployment step that applies the migrations
# also records which history it left in this database setting (backend/scripts/hosted_roles.py),
# derived from the database's own migration table; readiness compares it with this code's files.
SCHEMA_MARKER_SETTING = "mothli.schema_version"
_READINESS_CACHE_SECONDS = 10.0
_READINESS_TIMEOUT_SECONDS = 10.0


def schema_marker(rows: Iterable[tuple[int, str, str]]) -> str:
    """One short value naming a whole migration history: its last version and a digest of every
    (version, name, checksum). Equal only for the same files applied in the same order."""
    items = [(int(version), str(name), str(checksum)) for version, name, checksum in rows]
    digest = hashlib.sha256(
        "\n".join(f"{version}:{name}:{checksum}" for version, name, checksum in items).encode()
    ).hexdigest()[:16]
    return f"{items[-1][0] if items else 0:04d}-{digest}"


def expected_schema_marker() -> str:
    return schema_marker(
        (migration.version, migration.name, migration.checksum)
        for migration in discover_migrations()
    )


async def _read_schema_marker(dsn: str) -> str | None:
    async with await psycopg.AsyncConnection.connect(dsn, connect_timeout=5) as conn:
        row = await (
            await conn.execute("SELECT current_setting(%s, true)", (SCHEMA_MARKER_SETTING,))
        ).fetchone()
    return row[0] if row else None


class _HostedReadiness:
    """Ready only when the database answers as the runtime roles, each role holds exactly its
    reviewed privileges (the startup checks, run again), and the database's migration history is
    the one this code was built with. One check at a time, its answer reused for a few seconds, so
    a probe cannot be turned into load on the database. The answer carries no detail."""

    def __init__(self, *, read_marker: Callable[[], Awaitable[str | None]], repositories,
                 expected: str) -> None:
        self._read_marker = read_marker
        self._repositories = tuple(repositories)
        self._expected = expected
        self._lock = asyncio.Lock()
        self._checked_at: float | None = None
        self._ready = False

    async def ready(self) -> bool:
        async with self._lock:
            if (self._checked_at is not None
                    and monotonic() - self._checked_at < _READINESS_CACHE_SECONDS):
                return self._ready
            self._ready = await self._probe()
            self._checked_at = monotonic()
            return self._ready

    async def _probe(self) -> bool:
        # Nothing is logged (hosted logs carry only reviewed static messages, app/hosted/audit.py):
        # the deployment step (`docker compose run --rm migrate`) names the failing check.
        try:
            async with asyncio.timeout(_READINESS_TIMEOUT_SECONDS):
                if await self._read_marker() != self._expected:
                    return False
                for repository in self._repositories:
                    await repository.check_runtime_role()
        except Exception:  # noqa: BLE001 - any failure at all is "not ready"
            return False
        return True


_CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",
    "base-uri 'none'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "frame-src 'self' blob:",
    "form-action 'self'",
    "script-src 'self'",
    "script-src-attr 'none'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data: blob:",
    "media-src 'self' data: blob:",
    "connect-src 'self'",
    "worker-src 'self' blob:",
))


class _SecurityHeaders:
    """Apply browser defenses to API, static, and error responses.

    The CSP is a security boundary for Foliate's same-origin blob iframes: EPUB markup is untrusted
    and must not be allowed to execute inline or blob-hosted scripts in the application origin.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def add_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["content-security-policy"] = _CONTENT_SECURITY_POLICY
                headers["x-content-type-options"] = "nosniff"
                headers["referrer-policy"] = "no-referrer"
                headers["x-frame-options"] = "DENY"
                headers["permissions-policy"] = "camera=(), microphone=(), geolocation=()"
            await send(message)

        await self.app(scope, receive, add_headers)


class _ReadingToolTiming:
    """Measure tool requests without retaining book IDs, passages or dialogue."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        parts = scope.get("path", "").split("/")
        if scope["type"] != "http" or len(parts) != 5 or parts[1:3] != ["api", "books"] or parts[4] not in {"graph", "selection-action", "people"}:
            return await self.app(scope, receive, send)
        started, status = perf_counter(), 500
        async def observed(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
        try:
            await self.app(scope, receive, observed)
        finally:
            logging.getLogger("mothli.conversation").info(json.dumps({"event":"reading-tool",
                "operation":parts[4],"status":status,"elapsed_ms":round((perf_counter()-started)*1000,2)}))


class _LocalOriginGuard:
    """Reject foreign browser API requests before body parsing or cost-bearing GET handlers.

    Local mode is deliberately available to CLI clients without authentication. Host validation
    alone does not stop a foreign website from submitting a simple form to a loopback service.
    """

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _origin(value):
        try:
            parsed = urlsplit(value)
            scheme = parsed.scheme.casefold()
            if (scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path or parsed.query or parsed.fragment):
                return None
            port = parsed.port
            return scheme, parsed.hostname.casefold(), port if port is not None else (443 if scheme == "https" else 80)
        except (TypeError, ValueError):
            return None

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/api" or path.startswith("/api/")):
            await self.app(scope, receive, send)
            return
        origins = []
        hosts = []
        cross_site = False
        for name, value in scope.get("headers", ()):
            if name == b"origin":
                origins.append(value.decode("latin-1"))
            elif name == b"host":
                hosts.append(value.decode("latin-1"))
            elif name == b"sec-fetch-site" and value.strip().lower() == b"cross-site":
                cross_site = True
        if origins:
            request_origin = self._origin(f"{scope.get('scheme', 'http')}://{hosts[0]}") if len(hosts) == 1 else None
            foreign_origin = (len(origins) != 1 or request_origin is None
                              or self._origin(origins[0]) != request_origin)
        else:
            foreign_origin = False
        if cross_site or foreign_origin:
            response = JSONResponse({"detail": {
                "code": "local_origin_required",
                "message": "Open the local application to use this API.",
            }}, status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class _EpubUploadBodyLimit:
    """Bound multipart ingress before Starlette can spool an arbitrarily large upload to disk."""

    def __init__(self, app, *, max_file_bytes):
        self.app = app
        self.max_body_bytes = max_file_bytes + _MULTIPART_OVERHEAD_BYTES

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope.get("method") != "POST"
                or scope.get("path", "").rstrip("/") != "/api/books"):
            await self.app(scope, receive, send)
            return
        for key, value in scope.get("headers", ()):
            if key == b"content-length":
                try:
                    if int(value) > self.max_body_bytes:
                        await self._reject(scope, receive, send)
                        return
                except ValueError:
                    pass
                break
        # Buffer at most the policy limit before invoking Starlette's multipart parser. Most requests
        # stay in a small memory spool; large valid requests spill to a bounded temporary file. This
        # handles chunked bodies without relying on Content-Length or leaking parser-owned temp files.
        spool_memory = min(self.max_body_bytes, 1024 * 1024)
        with tempfile.SpooledTemporaryFile(max_size=spool_memory, mode="w+b") as body:
            received = 0
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    first = True

                    async def replay_disconnect():
                        nonlocal first
                        if first:
                            first = False
                            return message
                        return await receive()

                    await self.app(scope, replay_disconnect, send)
                    return
                chunk = message.get("body", b"")
                received += len(chunk)
                if received > self.max_body_bytes:
                    await self._reject(scope, receive, send)
                    return
                body.write(chunk)
                if not message.get("more_body", False):
                    break
            body.seek(0)

            async def replay_receive():
                chunk = body.read(64 * 1024)
                return {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": body.tell() < received,
                }

            await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope, receive, send):
        response = JSONResponse(
            {"detail": "EPUB upload exceeds the configured size limit"},
            status_code=413,
        )
        await response(scope, receive, send)


class _HostedNoStore:
    """Keep authenticated payloads and even identifier-specific failures out of shared caches."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return

        async def no_store_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["cache-control"] = "private, no-store"
                headers["pragma"] = "no-cache"
            await send(message)

        await self.app(scope, receive, no_store_send)


class _CredentialBodyLimit:
    """Bound secret-bearing JSON before parsing and before the application receives any bytes."""

    def __init__(self, app, *, max_body_bytes: int = _CREDENTIAL_BODY_BYTES):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
            (scope.get("method") == "POST" and path.rstrip("/") == "/api/credentials")
            or (scope.get("method") == "PUT" and path.startswith("/api/credentials/"))
        ):
            await self.app(scope, receive, send)
            return
        for key, value in scope.get("headers", ()):
            if key == b"content-length":
                try:
                    if int(value) > self.max_body_bytes:
                        await self._reject(scope, receive, send)
                        return
                except ValueError:
                    pass
                break
        chunks: list[bytes] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                first = True

                async def replay_disconnect():
                    nonlocal first
                    if first:
                        first = False
                        return message
                    return await receive()

                await self.app(scope, replay_disconnect, send)
                return
            chunk = message.get("body", b"")
            received += len(chunk)
            if received > self.max_body_bytes:
                await self._reject(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        position = 0

        async def replay_receive():
            nonlocal position
            if position >= len(chunks):
                return {"type": "http.request", "body": b"", "more_body": False}
            chunk = chunks[position]
            position += 1
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": position < len(chunks),
            }

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope, receive, send):
        response = JSONResponse(
            {"detail": "credential request exceeds the configured size limit"}, status_code=413
        )
        await response(scope, receive, send)


def create_app(
    settings: Settings | None = None,
    ingest_executor=None,
    *,
    auth_repository=None,
    tenant_repository=None,
    oidc_client=None,
    oidc_http_transport=None,
    auth_clock=None,
    hosted_runtime=None,
    object_storage=None,
    credential_cipher=None,
    provider_validator=None,
    schema_marker_reader=None,
) -> FastAPI:
    """``ingest_executor`` lets tests inject a deterministic inline executor; production uses the
    worker's own threadpool. ``schema_marker_reader`` lets tests stand in for the database read
    behind hosted readiness."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.deployment_mode == "hosted":
            repository = auth_repository or PostgresAuthRepository(
                settings.hosted_auth_dsn.get_secret_value()
            )
            tenant = tenant_repository or PostgresTenantRepository(
                settings.hosted_tenant_dsn.get_secret_value()
            )
            storage = None
            client = None
            runtime = None
            platform = None
            try:
                storage = object_storage or build_object_storage(settings)
                client = oidc_client or OIDCClient(
                    issuer=settings.oidc_issuer,
                    client_id=settings.oidc_client_id,
                    client_secret=settings.oidc_client_secret.get_secret_value(),
                    redirect_uri=settings.oidc_redirect_uri,
                    scopes=settings.oidc_scopes,
                    signing_algorithms=settings.oidc_signing_algorithm_values(),
                    clock_skew_seconds=settings.oidc_clock_skew_seconds,
                    timeout_seconds=settings.oidc_request_timeout_seconds,
                    transport=oidc_http_transport,
                )
                runtime = hosted_runtime or HostedRuntimeRegistry(
                    cache_max_entries=settings.hosted_runtime_cache_max_entries,
                    lock_max_entries=settings.hosted_runtime_lock_max_entries,
                )
                await repository.check_runtime_role()
                await tenant.check_runtime_role()
                cipher = credential_cipher or build_credential_cipher(settings)
                origins = allowed_origins(settings.hosted_provider_allowed_origins)
                validator = provider_validator or ProviderValidator(
                    timeout_seconds=settings.hosted_provider_validation_timeout_seconds
                )
                app.state.settings = settings
                app.state.auth_repository = repository
                app.state.tenant_repository = tenant
                app.state.oidc_client = client
                app.state.auth_clock = auth_clock or (lambda: datetime.now(UTC))
                app.state.hosted_runtime = runtime
                app.state.object_storage = storage
                app.state.credential_cipher = cipher
                app.state.provider_origins = origins
                app.state.provider_validator = validator
                # The companion's adapters (ADR 0041): bound to one reader per request, sharing
                # one bounded parse cache and one connection limit per process.
                set_connection_limit(settings.hosted_companion_db_connections)
                # The owner pays (ADR 0041 section 6): this process's caps, and the platform's
                # clients from the environment once anything is set to call a model. A missing or
                # refused setting stops startup, named and never printed.
                set_spend_limits(
                    reader_usd_per_day=settings.hosted_reader_usd_per_day,
                    reader_usd_per_month=settings.hosted_reader_usd_per_month,
                    platform_usd_per_day=settings.hosted_platform_usd_per_day,
                )
                if _platform_ai_configured(settings):
                    platform = build_platform_clients(settings)
                app.state.platform_client = platform
                app.state.companion_dsn = settings.hosted_tenant_dsn.get_secret_value()
                app.state.companion_sources = SourceParseCache(
                    settings.segmentation_cache_max_entries
                )
                tenant_dsn = settings.hosted_tenant_dsn.get_secret_value()
                app.state.readiness = _HostedReadiness(
                    read_marker=schema_marker_reader or (lambda: _read_schema_marker(tenant_dsn)),
                    repositories=(repository, tenant),
                    expected=expected_schema_marker(),
                )
                yield
            finally:
                drained = True
                if runtime is not None:
                    drained = await runtime.close(
                        timeout=settings.hosted_runtime_shutdown_timeout_seconds
                    )
                    if not drained:
                        stats = await runtime.stats()
                        _LOG.warning(
                            "hosted runtime shutdown timed out with %d active operations",
                            stats["active_lock_leases"],
                        )
                if storage is not None and drained:
                    storage.close()
                if client is not None and oidc_client is None:
                    await client.close()
                if getattr(platform, "jev", None) is not None:
                    platform.jev.close()
            return

        # LIT-24: restore swaps the whole data directory and therefore must never race a running app.
        # The lock file lives beside data_dir, so the protected inode is stable across a later swap.
        data_lock = DataDirLock(settings.data_dir)
        data_lock.acquire()
        try:
            state = deps.build_state(settings)         # raises -> startup refused (D-A7)
            # Zero-token credential preflight. A failure marks readiness degraded while preserving
            # access to already-derived Codex/reading data; no credential or provider detail is logged.
            state["client"].probe()
            for k, v in state.items():
                setattr(app.state, k, v)
            app.state.worker = IngestWorker(state["store"], state["catalog"], state["client"],
                                            settings, executor=ingest_executor)
            # asyncio.Lock instances are app-lifespan / single-event-loop scoped. Keeping the registry
            # here (rather than in the thread-safe worker) also gives every TestClient lifespan fresh locks.
            app.state.book_request_locks: dict[str, deps._BookRequestLockEntry] = {}
            app.state.recaps = RecapRegistry(
                max_entries=settings.recap_cache_max_entries,
                max_failures=settings.recap_failure_max_entries,
                max_flights=settings.recap_max_inflight,
                persist_path=(Path(settings.data_dir) / "reviewed-recaps-v1.json"
                              if settings.recap_disk_cache else None),
            )
            warmer = None
            if settings.recap_prewarm or settings.reader_prepare_enabled:
                from app.recap_warm import RecapPrewarmer
                warmer = RecapPrewarmer(app)
                app.state.worker.on_complete = warmer.notify
                for book in state["catalog"].list_books()[:8]:
                    if state["catalog"].high_water(book["book_id"]):
                        warmer.notify(book["book_id"], book["incarnation"])
            app.state.recap_warmer = warmer
            from app.chapter_notes import NotesBuilder, build_missing
            notes = app.state.notes_builder = NotesBuilder(lambda book_id: build_missing(
                book_id, state["catalog"], app.state.store, settings, app.state.reader_client,
                app.state.worker))
            from app.recollections import Recollector
            recollector = app.state.recollector = Recollector(
                state["catalog"], settings, lambda: app.state.reader_client)
            try:
                yield
            finally:
                notes.close()
                recollector.close()
                app.state.worker.shutdown()
                if warmer is not None:
                    await warmer.close()
                if getattr(state["client"], "jev", None) is not None:
                    state["client"].jev.close()
                app.state.store.close()
                app.state.catalog.close()
        finally:
            data_lock.release()

    docs_url = "/docs" if settings.expose_api_docs else None
    openapi_url = "/openapi.json" if settings.expose_api_docs else None
    app = FastAPI(
        title="Mothli",
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=None,
        openapi_url=openapi_url,
    )

    @app.exception_handler(ProviderUnavailable)
    async def provider_unavailable(_request, exc: ProviderUnavailable):
        return JSONResponse({"detail": exc.public_detail()}, status_code=503)

    @app.exception_handler(ModelMigrationRequired)
    async def model_migration_required(_request, exc: ModelMigrationRequired):
        return JSONResponse({"detail": exc.public_detail()}, status_code=409)

    if settings.deployment_mode == "hosted":
        # Hosted mode is intentionally partial: LIT-41 opens only the inventoried owner-scoped
        # read/reset routes. Storage, workers, and lifecycle operations remain fail-closed.
        @app.get("/api/health/live", tags=["health"])
        def hosted_live():
            return {"status": "ok"}

        @app.get("/api/health/ready", tags=["health"])
        async def hosted_ready():
            ready = await app.state.readiness.ready()
            return JSONResponse({"status": "ready" if ready else "not_ready"},
                                status_code=200 if ready else 503)

        app.include_router(auth_api.router)
        app.include_router(account_api.router)
        # Without reader keys the key routes are not mounted: they answer as unknown paths.
        app.include_router(tenant_api.hosted_router(reader_keys=settings.hosted_reader_keys))
        app.include_router(companion_routes.router)
        app.add_middleware(_EpubUploadBodyLimit, max_file_bytes=settings.epub_max_upload_bytes)
        if settings.hosted_reader_keys:
            app.add_middleware(_CredentialBodyLimit)
        app.add_middleware(_HostedNoStore)
    else:
        app.add_middleware(_ReadingToolTiming)
        app.add_middleware(_EpubUploadBodyLimit, max_file_bytes=settings.epub_max_upload_bytes)
        app.add_middleware(_LocalOriginGuard)
        app.include_router(health.router)
        app.include_router(books.router)
        app.include_router(classics.router)
        app.include_router(history.router)
        app.include_router(profile.router)
        app.include_router(reading.router)
        app.include_router(ingest.router)
        app.include_router(views.router)
    if settings.frontend_dist_dir and settings.deployment_mode == "local":
        # Mount last so every /api route keeps priority. StaticFiles validates the build directory
        # immediately, making a missing production build a startup/configuration error.
        app.mount("/", StaticFiles(directory=settings.frontend_dist_dir, html=True), name="frontend")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_host_values()))
    app.add_middleware(_SecurityHeaders)
    return app
