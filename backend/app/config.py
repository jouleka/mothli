"""Service configuration (ADR 0007 D-A7): pydantic-settings over the git-ignored repo-root ``.env``,
loaded via an ABSOLUTE ``__file__``-derived path (never a CWD lookup), fail-loud by default.

The fail-loud predicate is delegated to ``LLMClient``: at startup ``deps.build_state`` constructs the
client with ``allow_stub=settings.allow_stub``; if no real LLM provider resolves and ``ALLOW_STUB`` is
not set, the client RAISES and the app refuses to start (default-deny — a silent stub deploy would
produce garbage extractions). The key never leaves the process; settings are never logged wholesale.
"""
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# app/config.py -> app -> backend -> repo root (D-A7: parents[2], verified against the layout)
ENV_FILE = str(Path(__file__).resolve().parents[2] / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    # LLM / embeddings (all optional — the client auto-detects; embed_* are independent, D19)
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    openai_base_url: str | None = None
    embed_provider: str | None = None
    embed_base_url: str | None = None
    embed_api_key: str | None = None
    embed_model: str | None = None
    llm_cheap_model: str | None = None
    llm_large_model: str | None = None
    llm_transport: Literal["api", "codex-local"] = "api"

    # Local typed decisions; prose and extraction keep their configured generative provider.
    jev_enabled: bool = False
    typesafe_api_key: SecretStr | None = None
    jev_windows_credential: bool = False
    # 1.14.0 is the one version in use. 1.13.0 is still accepted so an older environment file
    # starts; on the repo's review controls it sent more correct answers to the slow review.
    jev_model: Literal["jev-1.13.0", "jev-1.14.0"] = "jev-1.14.0"
    jev_timeout_seconds: float = Field(default=5.0, gt=0, le=15)
    jev_selection_enabled: bool = True
    jev_evidence_enabled: bool = True
    jev_partial_evidence_enabled: bool = False
    jev_extraction_enabled: bool = False
    jev_identity_enabled: bool = False
    jev_recap_enabled: bool = False  # Experimental: conservative routing added latency in live QA.
    jev_min_probability: float = Field(default=0.95, ge=0.9, le=1)
    jev_min_confidence: float = Field(default=0.85, ge=0.8, le=1)
    recap_prewarm: bool = False
    recap_disk_cache: bool = False
    reader_answer_cache: bool = False
    reader_conversation_pipeline: bool = False
    reader_page_context: bool = False
    reader_memory_ingest: bool = True  # Local source-first preview can defer paid memory backfill.
    reader_conversation_jev: bool = False
    reader_conversation_routing: bool = False
    # One spoken reply instead of separate cited paragraphs. Same evidence and spoiler checks.
    reader_conversation_prose: bool = False
    # Streamed replies: one plain-prose writer, each sentence checked by Jev before it is shown.
    # Unset, it follows READER_CONVERSATION_JEV: on wherever that path is on. Set false to keep
    # the reviewed, non-streamed replies.
    reader_conversation_stream: bool | None = None
    # Both follow streamed replies when unset, the only path that uses them (and so the Jev
    # conversation setting). Set false to turn either off on its own.
    reader_story_notes: bool | None = None  # give replies Mothli's checked notes on finished chapters
    reader_read_map: bool | None = None     # Mothli follows what was actually read, not the furthest point
    # What Mothli remembers about the reader between conversations: a few short notes distilled
    # from the reader's own messages. Follows the read map when unset, which it needs: a note
    # about a book is used only while what was read when the reader said it is still read.
    reader_recollections: bool | None = None
    reader_conversation_draft_effort: Literal["low", "medium"] = "medium"
    reader_conversation_review_effort: Literal["low", "medium"] = "medium"
    # One model call in a page conversation. The provider default (120 s) is far past the
    # point where a reader has given up; codex-local keeps its own subprocess timeout.
    reader_conversation_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    reader_fast_mode: bool = False
    reader_reuse_enabled: bool = False
    reader_reuse_fast_review: bool = True  # Local GPT-6 Luna reuse checks only; normal preparation is unchanged.
    reader_reuse_min_probability: float = Field(default=0.99, ge=0.95, le=1)
    reader_reuse_min_confidence: float = Field(default=0.90, ge=0.85, le=1)
    reader_reuse_timeout_seconds: float = Field(default=0.65, gt=0, le=1)
    reader_prepare_enabled: bool = False
    reader_prepare_max_questions: int = Field(default=8, ge=1, le=10)
    reader_prepare_delay_seconds: float = Field(default=1.0, ge=0, le=10)
    reader_prepare_max_usd_per_book: float = Field(default=0.25, gt=0, le=2)

    # service
    deployment_mode: Literal["local", "hosted"] = "local"
    data_dir: str = str(Path(__file__).resolve().parents[2] / "data")
    frontend_dist_dir: str | None = None
    expose_api_docs: bool = False
    trusted_hosts: str = "localhost,127.0.0.1,testserver"
    allow_stub: bool = False                      # D-A7 default-deny
    vector_backend: Literal["vec0", "bruteforce"] = "vec0"
    store_max_handles: int = Field(default=16, ge=1)
    segmentation_cache_max_entries: int = Field(default=8, ge=1)
    recap_cache_max_entries: int = Field(default=128, ge=1)
    recap_failure_max_entries: int = Field(default=128, ge=1)
    recap_max_inflight: int = Field(default=8, ge=1)
    epub_max_upload_bytes: int = Field(default=128 * 1024 * 1024, ge=1, lt=2 ** 63)
    # "Pick a classic" downloads a public-domain book from Project Gutenberg when a reader asks for
    # one. Turn off for an offline or air-gapped install; nothing is fetched otherwise.
    classics_enabled: bool = True
    # The catalogue is every book that is public domain in the United States. Set this above 0 to
    # also require that every contributor died more than that many years ago (70 covers most
    # countries, 100 covers all of them); books with no recorded death year are then left out.
    classics_min_years_since_death: int = Field(default=0, ge=0, le=200)
    cost_max_input_tokens_per_call: int = Field(default=60_000, ge=1, lt=2 ** 63)
    cost_max_output_tokens_per_call: int = Field(default=4_096, ge=1, lt=2 ** 63)
    # Long-book ingestion can consume the old 2M default before reader preparation starts.
    # Dollar/per-call/background caps still apply; an explicit deployment override wins.
    cost_max_input_tokens_per_book: int = Field(default=8_000_000, ge=1, lt=2 ** 63)
    cost_max_output_tokens_per_book: int = Field(default=500_000, ge=1, lt=2 ** 63)
    cost_max_usd_per_book: float = Field(default=5.0, gt=0, allow_inf_nan=False)

    # Hosted authentication (LIT-40). DSNs and client secrets use SecretStr so validation errors,
    # reprs, and diagnostics cannot accidentally print credentials.
    hosted_auth_dsn: SecretStr | None = None
    hosted_tenant_dsn: SecretStr | None = None
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    oidc_redirect_uri: str | None = None
    oidc_scopes: str = "openid profile email"
    oidc_signing_algorithms: str = "RS256"
    oidc_request_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    oidc_clock_skew_seconds: int = Field(default=60, ge=0, le=300)
    oidc_attempt_ttl_seconds: int = Field(default=600, ge=60, le=1800)
    session_absolute_ttl_seconds: int = Field(default=8 * 60 * 60, ge=300, le=30 * 24 * 60 * 60)
    session_idle_ttl_seconds: int = Field(default=30 * 60, ge=60, le=24 * 60 * 60)
    hosted_runtime_cache_max_entries: int = Field(default=256, ge=1, le=100_000)
    hosted_runtime_lock_max_entries: int = Field(default=256, ge=1, le=100_000)
    hosted_runtime_shutdown_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    hosted_storage_backend: Literal["filesystem", "s3"] | None = None
    hosted_storage_filesystem_root: str | None = None
    hosted_storage_filesystem_key: SecretStr | None = None
    hosted_s3_bucket: str | None = None
    hosted_s3_region: str = "us-east-1"
    hosted_s3_endpoint_url: str | None = None
    hosted_s3_allow_insecure_http: bool = False
    hosted_s3_access_key_id: SecretStr | None = None
    hosted_s3_secret_access_key: SecretStr | None = None
    hosted_s3_sse_algorithm: Literal["AES256", "aws:kms"] = "AES256"
    hosted_s3_kms_key_id: str | None = None
    hosted_s3_expected_bucket_owner: str | None = None
    # LIT-45 envelope-encryption keyring. The active key encrypts only per-credential data keys;
    # previous version->base64 keys remain available solely while envelopes are being rewrapped.
    hosted_credential_master_key: SecretStr | None = None
    hosted_credential_key_version: str | None = None
    hosted_credential_previous_master_keys: SecretStr | None = None
    hosted_provider_allowed_origins: str = (
        "https://api.openai.com,https://api.anthropic.com"
    )
    hosted_provider_validation_timeout_seconds: float = Field(default=5.0, gt=0, le=15)
    # The most database connections the hosted companion adapters hold at once in one process
    # (ADR 0041 section 2): a burst of page reports cannot exceed Postgres max_connections.
    hosted_companion_db_connections: int = Field(default=20, ge=1, le=1000)
    # The owner pays for the companion's AI in hosted mode (ADR 0041 section 6). The amounts are
    # DEFAULTS TO BE CONFIRMED BY THE OWNER: per reader per UTC day and per UTC month (used where
    # the reader's owner_limits row leaves its own cap NULL), and for the whole platform per UTC
    # day, whatever any reader does.
    hosted_reader_usd_per_day: float = Field(default=0.10, ge=0, le=1000, allow_inf_nan=False)
    hosted_reader_usd_per_month: float = Field(default=2.00, ge=0, le=10000, allow_inf_nan=False)
    hosted_platform_usd_per_day: float = Field(default=5.00, ge=0, le=100000, allow_inf_nan=False)
    # The older "paste your own key" routes (/api/credentials, /api/provider-settings and the
    # reader-key reply paths). Off: they answer as if they did not exist.
    hosted_reader_keys: bool = False

    @model_validator(mode="after")
    def validate_hosted_auth(self):
        if not self.reader_memory_ingest and (self.deployment_mode != "local" or not self.reader_page_context):
            raise ValueError("deferred memory ingestion requires local page context")
        if self.reader_conversation_stream is None:
            self.reader_conversation_stream = self.reader_conversation_jev
        if self.reader_conversation_stream and not self.reader_conversation_jev:
            raise ValueError("READER_CONVERSATION_STREAM requires READER_CONVERSATION_JEV")
        if self.reader_conversation_prose and not self.reader_conversation_jev:
            raise ValueError("prose replies require the local conversation Jev pipeline")
        if self.reader_conversation_routing and not self.reader_conversation_jev:
            raise ValueError("turn routing requires the local conversation Jev pipeline")
        # Hosted mode may run the conversation with Jev for the streamed chat inside a book, on
        # the conditions checked below with the rest of hosted mode (ADR 0041 part 5).
        if self.reader_conversation_jev and (not self.reader_page_context or not self.jev_enabled):
            raise ValueError("conversation Jev experiment requires local page context and Jev")
        # After the Jev check, so a server missing page context is told that, not about the map.
        if self.reader_read_map is None:
            self.reader_read_map = self.reader_conversation_stream
        if self.reader_story_notes is None:
            self.reader_story_notes = self.reader_conversation_stream
        # Hosted mode may keep the read map without streamed replies: it sends nothing to any
        # model (ADR 0041 part 1). Locally it still needs them.
        hosted = self.deployment_mode == "hosted"
        if self.reader_read_map and not (self.reader_page_context
                                         and (self.reader_conversation_stream or hosted)):
            raise ValueError("READER_READ_MAP requires READER_PAGE_CONTEXT and streamed replies")
        if hosted and (self.reader_story_notes or self.reader_recollections):
            raise ValueError("chapter notes and recollections are not available in hosted mode yet")
        if self.reader_recollections is None:
            self.reader_recollections = self.reader_read_map and not hosted
        if self.reader_recollections and not self.reader_read_map:
            raise ValueError("READER_RECOLLECTIONS requires READER_READ_MAP")
        if self.llm_transport == "codex-local" and self.deployment_mode != "local":
            raise ValueError("codex-local is only supported for local testing")
        if self.deployment_mode != "hosted":
            return self

        hosts = self.trusted_host_values()
        if not hosts or "*" in hosts:
            raise ValueError("hosted mode requires an explicit TRUSTED_HOSTS allowlist")

        required = {
            "HOSTED_AUTH_DSN": self.hosted_auth_dsn,
            "HOSTED_TENANT_DSN": self.hosted_tenant_dsn,
            "OIDC_ISSUER": self.oidc_issuer,
            "OIDC_CLIENT_ID": self.oidc_client_id,
            "OIDC_CLIENT_SECRET": self.oidc_client_secret,
            "OIDC_REDIRECT_URI": self.oidc_redirect_uri,
            "HOSTED_CREDENTIAL_MASTER_KEY": self.hosted_credential_master_key,
            "HOSTED_CREDENTIAL_KEY_VERSION": self.hosted_credential_key_version,
        }
        missing = [name for name, value in required.items() if value is None or value == ""]
        if missing:
            raise ValueError("hosted mode requires " + ", ".join(missing))
        # ADR 0041 section 7: the owner pays, so nothing but the owner's own paid API accounts,
        # and nothing that writes one file shared by every reader under DATA_DIR.
        refused = {
            "ALLOW_STUB": self.allow_stub,
            "JEV_WINDOWS_CREDENTIAL": self.jev_windows_credential,
            "READER_ANSWER_CACHE": self.reader_answer_cache,
            "RECAP_DISK_CACHE": self.recap_disk_cache,
            "READER_PREPARE_ENABLED": self.reader_prepare_enabled,
        }
        on = [name for name, value in refused.items() if value]
        if on:
            raise ValueError("hosted mode refuses " + ", ".join(on))
        # ADR 0041 part 5: the streamed chat inside a book, paid by the owner, only with the
        # platform's own writer and Jev keys, the read map (hosted keeps no confirmed anchor), and
        # caps the operator has set rather than inherited (no unbounded owner-paid AI).
        if self.reader_conversation_jev:
            needed = {
                "READER_READ_MAP": self.reader_read_map,
                "TYPESAFE_API_KEY": (self.typesafe_api_key is not None
                                     and bool(self.typesafe_api_key.get_secret_value())),
                "OPENAI_API_KEY or ANTHROPIC_API_KEY": bool(self.openai_api_key
                                                            or self.anthropic_api_key),
                "HOSTED_READER_USD_PER_DAY": "hosted_reader_usd_per_day" in self.model_fields_set,
                "HOSTED_PLATFORM_USD_PER_DAY": "hosted_platform_usd_per_day" in self.model_fields_set,
            }
            missing = [name for name, value in needed.items() if not value]
            if missing:
                raise ValueError("the streamed chat in hosted mode requires " + ", ".join(missing))
        if self.openai_base_url:
            parsed = urlsplit(self.openai_base_url.strip())
            allowed = {item.strip().rstrip("/").casefold()
                       for item in self.hosted_provider_allowed_origins.split(",") if item.strip()}
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                    or parsed.password or f"https://{parsed.netloc.casefold()}" not in allowed):
                raise ValueError(
                    "OPENAI_BASE_URL must be an HTTPS address whose origin is in "
                    "HOSTED_PROVIDER_ALLOWED_ORIGINS")

        assert self.oidc_issuer is not None
        assert self.oidc_redirect_uri is not None
        for label, value in (
            ("OIDC_ISSUER", self.oidc_issuer),
            ("OIDC_REDIRECT_URI", self.oidc_redirect_uri),
        ):
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError(f"{label} must be an HTTPS URL without user information")
            if parsed.fragment or (label == "OIDC_ISSUER" and parsed.query):
                raise ValueError(f"{label} may not contain a query or fragment")
        if self.oidc_issuer.endswith("/"):
            raise ValueError("OIDC_ISSUER must use its exact issuer value without a trailing slash")
        if "openid" not in self.oidc_scopes.split():
            raise ValueError("OIDC_SCOPES must include openid")
        algorithms = self.oidc_signing_algorithm_values()
        if not algorithms or "none" in {item.lower() for item in algorithms}:
            raise ValueError("OIDC_SIGNING_ALGORITHMS must pin at least one signed algorithm")
        if self.session_idle_ttl_seconds > self.session_absolute_ttl_seconds:
            raise ValueError("SESSION_IDLE_TTL_SECONDS may not exceed absolute session lifetime")
        if self.hosted_storage_backend == "filesystem":
            if not self.hosted_storage_filesystem_root or self.hosted_storage_filesystem_key is None:
                raise ValueError(
                    "filesystem object storage requires HOSTED_STORAGE_FILESYSTEM_ROOT and "
                    "HOSTED_STORAGE_FILESYSTEM_KEY"
                )
        elif self.hosted_storage_backend == "s3":
            if not self.hosted_s3_bucket:
                raise ValueError("S3 object storage requires HOSTED_S3_BUCKET")
            if (self.hosted_s3_access_key_id is None) != (
                self.hosted_s3_secret_access_key is None
            ):
                raise ValueError("S3 static access key id and secret must be configured together")
            if self.hosted_s3_sse_algorithm == "aws:kms" and not self.hosted_s3_kms_key_id:
                raise ValueError("S3 aws:kms encryption requires HOSTED_S3_KMS_KEY_ID")
            if self.hosted_s3_sse_algorithm == "AES256" and self.hosted_s3_kms_key_id:
                raise ValueError("S3 AES256 encryption cannot use HOSTED_S3_KMS_KEY_ID")
            if self.hosted_s3_endpoint_url:
                endpoint = urlsplit(self.hosted_s3_endpoint_url)
                allowed_schemes = {"https"}
                if self.hosted_s3_allow_insecure_http:
                    allowed_schemes.add("http")
                if (
                    endpoint.scheme not in allowed_schemes
                    or not endpoint.hostname
                    or endpoint.username
                    or endpoint.password
                    or endpoint.query
                    or endpoint.fragment
                ):
                    raise ValueError("HOSTED_S3_ENDPOINT_URL must be an allowed origin URL")
        return self

    def oidc_signing_algorithm_values(self) -> tuple[str, ...]:
        return tuple(item.strip() for item in self.oidc_signing_algorithms.split(",") if item.strip())

    def trusted_host_values(self) -> tuple[str, ...]:
        return tuple(item.strip() for item in self.trusted_hosts.split(",") if item.strip())

    def llm_env(self) -> dict:
        """The env mapping handed to ``LLMClient(env=...)`` — settings values only (the process env is
        deliberately NOT consulted twice; one source of truth)."""
        pairs = {
            "OPENAI_API_KEY": self.openai_api_key,
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
            "OPENAI_BASE_URL": self.openai_base_url,
            "EMBED_PROVIDER": self.embed_provider,
            "EMBED_BASE_URL": self.embed_base_url,
            "EMBED_API_KEY": self.embed_api_key,
            "EMBED_MODEL": self.embed_model,
            "LLM_CHEAP_MODEL": self.llm_cheap_model,
            "LLM_LARGE_MODEL": self.llm_large_model,
            "LLM_TRANSPORT": self.llm_transport,
        }
        return {k: v for k, v in pairs.items() if v}
