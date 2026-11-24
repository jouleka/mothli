"""Service configuration (ADR 0007 D-A7): pydantic-settings over the git-ignored repo-root ``.env``,
loaded via an ABSOLUTE ``__file__``-derived path (never a CWD lookup), fail-loud by default.

The fail-loud predicate is delegated to ``LLMClient``: at startup ``deps.build_state`` constructs the
client with ``allow_stub=settings.allow_stub``; if no real LLM provider resolves and ``ALLOW_STUB`` is
not set, the client RAISES and the app refuses to start (default-deny — a silent stub deploy would
produce garbage extractions). The key never leaves the process; settings are never logged wholesale.
"""
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# app/config.py -> app -> backend -> repo root (D-A7: parents[2], verified against the layout)
ENV_FILE = str(Path(__file__).resolve().parents[2] / ".env")
# Paddle's ids are a prefix and 26 lowercase letters or digits; its notification destination's
# secret key starts pdl_ntfset_ (developer.paddle.com/webhooks/signature-verification). Nothing
# more is required of the key than is certain: its second part is base64-like (Paddle's own SDK
# example holds a "+"), so any characters but whitespace, at a sane length.
_PADDLE_PRICE = re.compile(r"pri_[a-z0-9]{26}")
_PADDLE_SECRET = re.compile(r"pdl_ntfset_\S{16,500}")
# An API key starts pdl_, then sdbx_ or live_ for its environment, then apikey_
# (developer.paddle.com/api-reference/about/authentication); the rest is checked as loosely.
_PADDLE_API_KEY = {"sandbox": re.compile(r"pdl_sdbx_apikey_\S{16,500}"),
                   "production": re.compile(r"pdl_live_apikey_\S{16,500}")}
# A client-side token starts test_ (sandbox) or live_ (developer.paddle.com/paddle-js/about/
# client-side-tokens); the rest is checked as loosely.
_PADDLE_CLIENT_TOKEN = {"sandbox": re.compile(r"test_\S{16,200}"),
                        "production": re.compile(r"live_\S{16,200}")}
# The operator's details are printed on the public pages as they are: one line of text with no
# markup characters, and an address of the plain local@domain.tld shape. The deployment
# template's placeholder is refused, so a copied template never publishes it as a name.
_OPERATOR_TEXT = re.compile(r"[^\x00-\x1f\x7f<>{}\\]{2,}")
_OPERATOR_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*"
                             r"\.[A-Za-z]{2,63}")
_PLACEHOLDER = "OWNER_MUST_FILL_IN"


def _items(value: str) -> tuple[str, ...]:
    """A comma-separated setting's values, stripped, empty ones dropped."""
    return tuple(item.strip() for item in value.split(",") if item.strip())


class Settings(BaseSettings):
    # A refused setting is reported by name only: the message reaches the log, and values are secrets.
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore",
                                      hide_input_in_errors=True)

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
    # A reader stays signed in on their own device for 30 days at most, and is signed out after
    # 14 days without touching Mothli (ADR 0020, 2026-11-24). Idle may not exceed absolute.
    session_absolute_ttl_seconds: int = Field(
        default=30 * 24 * 60 * 60, ge=300, le=30 * 24 * 60 * 60
    )
    session_idle_ttl_seconds: int = Field(default=14 * 24 * 60 * 60, ge=60, le=30 * 24 * 60 * 60)
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
    # The origins the platform's writer address (OPENAI_BASE_URL) may point at in hosted mode: the
    # owner's writer key is sent there with every reply. (Readers' own keys, and the master key
    # that wrapped them, were removed: ADR 0043.)
    hosted_provider_allowed_origins: str = (
        "https://api.openai.com,https://api.anthropic.com"
    )
    # The most database connections the hosted companion adapters hold at once in one process
    # (ADR 0041 section 2): a burst of page reports cannot exceed Postgres max_connections.
    hosted_companion_db_connections: int = Field(default=20, ge=1, le=1000)
    # The owner pays for the companion's AI in hosted mode (ADR 0041 section 6): per reader per
    # UTC day and per UTC month (used where the reader's owner_limits row leaves its own cap
    # NULL), and for the whole platform per UTC day, whatever any reader does. A paying reader's
    # two hidden guards, the owner's numbers (ADR 0045 section 4, decided 23 November 2026):
    # $1.00 a day and $4.00 a month, about 47 and 190 replies at 2.1 cents. Never shown.
    hosted_reader_usd_per_day: float = Field(default=1.00, ge=0, le=1000, allow_inf_nan=False)
    hosted_reader_usd_per_month: float = Field(default=4.00, ge=0, le=10000, allow_inf_nan=False)
    hosted_platform_usd_per_day: float = Field(default=15.00, ge=0, le=100000, allow_inf_nan=False)
    # ADR 0045 step 1: a reader's two free weeks with Mothli, from their first question. While
    # they last the reader's day and month above are not applied; one ceiling in dollars of their
    # own chat spend since the start is (about 100 replies at 2.1 cents). Never shown to a reader.
    hosted_free_days: int = Field(default=14, ge=1, le=365)
    hosted_free_usd: float = Field(default=2.10, ge=0, le=10000, allow_inf_nan=False)
    # ADR 0045 step 3 (section 4, decided 23 November 2026): a subscriber's first paid month,
    # about 300 replies. For the 30 days after their first Paddle plan began, once per account,
    # their chat since then is held to this in place of the month above; the day stays. A gift
    # has none. Never shown to a reader.
    hosted_first_paid_month_usd: float = Field(default=6.30, ge=0, le=10000, allow_inf_nan=False)
    # ADR 0041 section 6, amended 20 November 2026: a reader's allowance counts only their
    # conversation. Mothli's background work for a reader (their chapter notes, what it remembers
    # of them) is Mothli's cost: the budget of all readers' background work together per UTC day,
    # and the guards against abuse per reader per UTC day and month, which a reader who is really
    # reading never meets (60 chapters marked read on day one use about $0.42 of the $1.00 day).
    # Past either the work waits for the next UTC day without a word to the reader.
    hosted_background_usd_per_day: float = Field(default=3.00, ge=0, le=100000,
                                                 allow_inf_nan=False)
    hosted_reader_background_usd_per_day: float = Field(default=1.00, ge=0, le=1000,
                                                        allow_inf_nan=False)
    hosted_reader_background_usd_per_month: float = Field(default=5.00, ge=0, le=10000,
                                                          allow_inf_nan=False)
    # ADR 0045 section 5, step 6: background work by where a reader stands with their plan. As
    # above in the free weeks and for paying readers; none once their free weeks are over and
    # they do not pay. A reader who has signed up and never asked Mothli anything gets a bounded
    # warm-up so their first session is not cold: background work within this many days of
    # sign-up, up to this many chapters of story memory their reading is the first to build, and
    # up to this much of chapter notes, all counted over the whole time they have not asked. Past
    # any of the three it stops, silently, until their first question. Never shown to a reader.
    hosted_warmup_days: int = Field(default=30, ge=0, le=365)
    hosted_warmup_new_chapters: int = Field(default=25, ge=0, le=100000)
    hosted_warmup_notes_usd: float = Field(default=0.50, ge=0, le=1000, allow_inf_nan=False)
    # ADR 0042 sections 3 and 6, ADR 0044: story memory in hosted mode, built by the worker's
    # story_memory jobs for every reader's book, one chapter ahead of their reading, once per book
    # file. HOSTED_STORY_MEMORY turns the feature on at all.
    hosted_story_memory: bool = False
    # ADR 0044 section 5: Mothli pays for story memory, recorded against the notes account and
    # never against a reader. Its day ceiling, for everyone (used where the notes account's
    # owner_limits row leaves its own day cap NULL, so a fresh deployment needs no step), and the
    # guards against abuse, which a reader who is really reading never meets: how many new
    # chapters one reader's runs may be the first to build in a UTC day and in a UTC month, and
    # of how many new files a month they may be the first to build chapter 1. Settings, to be
    # raised if a real reader ever reaches one.
    hosted_story_memory_usd_per_day: float = Field(default=10.00, ge=0, le=100000,
                                                   allow_inf_nan=False)
    hosted_story_memory_new_chapters_per_day: int = Field(default=30, ge=0, le=100000)
    hosted_story_memory_new_chapters_per_month: int = Field(default=300, ge=0, le=1000000)
    hosted_story_memory_new_files_per_month: int = Field(default=5, ge=0, le=100000)
    # ADR 0045 step 3: Paddle's messages (POST /api/billing/paddle). With no PADDLE_WEBHOOK_SECRET
    # the route does not exist. The secret is the notification destination's (Paddle shows it
    # once); the price ids are Mothli's own, each setting one id or several separated by commas
    # (a price replaced in Paddle stays here while readers keep it). Paddle's account is shared
    # with another product of the owner's: a message about any other price is answered and kept
    # nowhere. HOSTED_BILLING_DSN is the billing role, the only one that writes a reader's plan.
    paddle_environment: Literal["sandbox", "production"] = "sandbox"
    paddle_webhook_secret: SecretStr | None = None
    paddle_price_monthly: str = ""
    paddle_price_yearly: str = ""
    # How far a message's signed time may be from the server's clock. Paddle's own libraries
    # allow five seconds; the repeat check by event id also stops a replay.
    paddle_webhook_tolerance_seconds: int = Field(default=300, ge=5, le=3600)
    hosted_billing_dsn: SecretStr | None = None
    # ADR 0045 step 4: Paddle's API key, for subscribing, cancelling, keeping a plan, the
    # customer portal and the regular check (app/hosted/subscribe.py). With none, those routes do
    # not exist. It needs Paddle's messages too: a payment no message reports would make no one
    # paying.
    paddle_api_key: SecretStr | None = None
    # ADR 0045 step 4, the screens: Paddle's client-side token, which Paddle.js in the reader's
    # browser uses to show prices and open checkout. Public by design ("safe to publish",
    # developer.paddle.com/paddle-js/about/client-side-tokens), so not a secret, but still the
    # owner's setting. With none, the plan sheet offers no checkout.
    paddle_client_token: str = ""
    # Mothli's own page for Paddle's payment links (ADR 0045, "One Paddle account, several
    # products"): every transaction the server makes carries it as ``checkout.url``, so Paddle's
    # emails about a checkout send the buyer here and not to the account's default payment link,
    # which is the other product's page. This app's own address: https, a host in TRUSTED_HOSTS,
    # the app's front page (no path, query or fragment). Paddle refuses it until its domain is
    # approved under Website approval. Required with PADDLE_API_KEY.
    paddle_checkout_url: str = ""
    # ADR 0045 step 8: who runs Mothli, for the public pages (the terms, privacy, refunds and
    # contact pages a visitor and Paddle's reviewers read signed out). The legal name and country
    # of the operator, and the address readers write to. Never written in the app: with any of
    # the three missing, the pages say Mothli is not open for sign-ups here, and the server
    # refuses PADDLE_API_KEY (a deployment that can take payments must publish its terms).
    operator_legal_name: str = ""
    operator_country: str = ""
    operator_contact_email: str = ""

    @model_validator(mode="after")
    def validate_hosted_auth(self):
        # Hosted mode with neither of the platform's AI accounts (no writer key and no TypeSafe
        # key; a line left empty counts as none) runs without Mothli's chat, whatever the chat's
        # switches say: the server starts, readers read, and the app is told there is no chat
        # (``companion_chat``), so it shows none. One account without the other is refused below.
        # Story memory, paid through the same accounts, is off with it (ADR 0044 step 6: the
        # deployment template turns it on).
        if self.deployment_mode == "hosted" and not (
                self.openai_api_key or self.anthropic_api_key
                or (self.typesafe_api_key and self.typesafe_api_key.get_secret_value())):
            self.openai_api_key = self.anthropic_api_key = self.typesafe_api_key = None
            self.jev_enabled = self.reader_conversation_jev = False
            self.reader_conversation_stream = False
            self.hosted_story_memory = False
        if not self.reader_memory_ingest and (self.deployment_mode != "local" or not self.reader_page_context):
            raise ValueError("deferred memory ingestion requires local page context")
        if self.reader_conversation_stream is None:
            self.reader_conversation_stream = self.reader_conversation_jev
        if self.reader_conversation_stream and not self.reader_conversation_jev:
            raise ValueError("READER_CONVERSATION_STREAM requires READER_CONVERSATION_JEV")
        # Hosted mode has no reply but the streamed one (the non-streamed reply was removed, ADR
        # 0043): the chat's paid settings with streaming off would pay for notes and story memory
        # while no reader could ever be answered.
        if (self.deployment_mode == "hosted" and self.reader_conversation_jev
                and not self.reader_conversation_stream):
            raise ValueError(
                "hosted mode answers readers only with streamed replies: READER_CONVERSATION_JEV "
                "is on and READER_CONVERSATION_STREAM is false; remove READER_CONVERSATION_STREAM "
                "(it follows READER_CONVERSATION_JEV) or set it to true")
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
        # ADR 0041 parts 6 and 7: chapter notes and recollections run in hosted mode as durable
        # jobs, with the streamed chat's conditions (they call the same paid writer and checks).
        # Unset, recollections follow the read map, and in hosted mode the chat as well.
        if hosted and self.reader_recollections and not self.reader_conversation_jev:
            raise ValueError("recollections in hosted mode require READER_CONVERSATION_JEV")
        if hosted and self.reader_story_notes and not self.reader_conversation_jev:
            raise ValueError("chapter notes in hosted mode require READER_CONVERSATION_JEV")
        if self.reader_recollections is None:
            self.reader_recollections = bool(self.reader_read_map
                                             and (self.reader_conversation_jev or not hosted))
        if self.reader_recollections and not self.reader_read_map:
            raise ValueError("READER_RECOLLECTIONS requires READER_READ_MAP")
        if self.llm_transport == "codex-local" and self.deployment_mode != "local":
            raise ValueError("codex-local is only supported for local testing")
        # ADR 0042: hosted story memory, which local mode does not use (it builds story memory
        # itself); the extraction is paid by the owner, as the chat is.
        if self.hosted_story_memory and not hosted:
            raise ValueError("HOSTED_STORY_MEMORY is a hosted-mode setting")
        if self.hosted_story_memory and not self.reader_conversation_jev:
            raise ValueError("story memory in hosted mode requires READER_CONVERSATION_JEV")
        # ADR 0045 step 3: Paddle's messages. A line left empty is no secret.
        if self.paddle_webhook_secret is not None \
                and not self.paddle_webhook_secret.get_secret_value():
            self.paddle_webhook_secret = None
        for label, value in (("PADDLE_PRICE_MONTHLY", self.paddle_price_monthly),
                             ("PADDLE_PRICE_YEARLY", self.paddle_price_yearly)):
            if any(not _PADDLE_PRICE.fullmatch(item) for item in _items(value)):
                raise ValueError(f"{label} must be Paddle price ids (pri_...), separated by commas")
        if set(_items(self.paddle_price_monthly)) & set(_items(self.paddle_price_yearly)):
            raise ValueError("PADDLE_PRICE_MONTHLY and PADDLE_PRICE_YEARLY share a price id")
        if self.paddle_webhook_secret is not None:
            if not hosted:
                raise ValueError("PADDLE_WEBHOOK_SECRET is a hosted-mode setting")
            if not _PADDLE_SECRET.fullmatch(self.paddle_webhook_secret.get_secret_value()):
                raise ValueError("PADDLE_WEBHOOK_SECRET must be the notification destination's "
                                 "secret key as Paddle shows it (pdl_ntfset_...)")
            missing = [name for name, value in (
                ("HOSTED_BILLING_DSN", self.hosted_billing_dsn),
                ("PADDLE_PRICE_MONTHLY or PADDLE_PRICE_YEARLY", self.paddle_prices()))
                if not value]
            if missing:
                raise ValueError("Paddle's messages require " + ", ".join(missing))
        # ADR 0045 step 4: Paddle's API. A line left empty is no key.
        if self.paddle_api_key is not None and not self.paddle_api_key.get_secret_value():
            self.paddle_api_key = None
        if self.paddle_api_key is not None:
            if self.paddle_webhook_secret is None:
                raise ValueError("PADDLE_API_KEY requires PADDLE_WEBHOOK_SECRET (Paddle's "
                                 "messages are what make a subscriber paying)")
            if not _PADDLE_API_KEY[self.paddle_environment].fullmatch(
                    self.paddle_api_key.get_secret_value()):
                raise ValueError("PADDLE_API_KEY must be an API key from Paddle's "
                                 f"{self.paddle_environment} (PADDLE_ENVIRONMENT), as Paddle "
                                 "shows it")
        if self.paddle_client_token:
            if self.paddle_api_key is None:
                raise ValueError("PADDLE_CLIENT_TOKEN requires PADDLE_API_KEY (the server makes "
                                 "every checkout)")
            if not _PADDLE_CLIENT_TOKEN[self.paddle_environment].fullmatch(
                    self.paddle_client_token):
                raise ValueError("PADDLE_CLIENT_TOKEN must be a client-side token from Paddle's "
                                 f"{self.paddle_environment} (PADDLE_ENVIRONMENT), as Paddle "
                                 "shows it")
        if self.paddle_checkout_url and self.paddle_api_key is None:
            raise ValueError("PADDLE_CHECKOUT_URL requires PADDLE_API_KEY (the server makes "
                             "every checkout)")
        if self.paddle_api_key is not None and not self.paddle_checkout_url:
            raise ValueError("PADDLE_API_KEY requires PADDLE_CHECKOUT_URL (Mothli's own page for "
                             "Paddle's payment links; without it they go to the account's "
                             "default page)")
        # ADR 0045 step 8: the operator's details, as the public pages print them.
        self.operator_legal_name = self.operator_legal_name.strip()
        self.operator_country = self.operator_country.strip()
        self.operator_contact_email = self.operator_contact_email.strip()
        for label, value, longest in (("OPERATOR_LEGAL_NAME", self.operator_legal_name, 200),
                                      ("OPERATOR_COUNTRY", self.operator_country, 100)):
            if value and (len(value) > longest or not _OPERATOR_TEXT.fullmatch(value)
                          or _PLACEHOLDER in value.upper()):
                raise ValueError(f"{label} must be plain text of at most {longest} characters "
                                 "on one line, as it should appear on Mothli's terms")
        if self.operator_contact_email and (
                len(self.operator_contact_email) > 254
                or not _OPERATOR_EMAIL.fullmatch(self.operator_contact_email)
                or _PLACEHOLDER in self.operator_contact_email.upper()):
            raise ValueError("OPERATOR_CONTACT_EMAIL must be one email address that readers "
                             "can write to")
        if self.paddle_api_key is not None and not self.operator_published():
            raise ValueError("PADDLE_API_KEY requires OPERATOR_LEGAL_NAME, OPERATOR_COUNTRY and "
                             "OPERATOR_CONTACT_EMAIL (a server that can take payments must "
                             "publish who runs it in its terms)")
        if self.deployment_mode != "hosted":
            return self

        hosts = self.trusted_host_values()
        if not hosts or "*" in hosts:
            raise ValueError("hosted mode requires an explicit TRUSTED_HOSTS allowlist")
        if self.paddle_checkout_url:
            page = urlsplit(self.paddle_checkout_url)
            if (page.scheme != "https" or not page.hostname or page.username or page.password
                    or page.port is not None or page.path not in ("", "/") or page.query
                    or page.fragment or "?" in self.paddle_checkout_url
                    or "#" in self.paddle_checkout_url
                    or any(character.isspace() for character in self.paddle_checkout_url)
                    or page.hostname not in {host.casefold() for host in hosts}):
                raise ValueError("PADDLE_CHECKOUT_URL must be this app's own front page: "
                                 "https://, a host in TRUSTED_HOSTS, no path, query or fragment")

        required = {
            "HOSTED_AUTH_DSN": self.hosted_auth_dsn,
            "HOSTED_TENANT_DSN": self.hosted_tenant_dsn,
            "OIDC_ISSUER": self.oidc_issuer,
            "OIDC_CLIENT_ID": self.oidc_client_id,
            "OIDC_CLIENT_SECRET": self.oidc_client_secret,
            "OIDC_REDIRECT_URI": self.oidc_redirect_uri,
        }
        missing = [name for name, value in required.items() if value is None or value == ""]
        if missing:
            raise ValueError("hosted mode requires " + ", ".join(missing))
        # ADR 0041 section 7: the owner pays, so nothing but the owner's own paid API accounts.
        # (The settings that wrote one file shared by every reader were removed, ADR 0043.)
        refused = {
            "ALLOW_STUB": self.allow_stub,
            "JEV_WINDOWS_CREDENTIAL": self.jev_windows_credential,
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
        if self.oidc_issuer.endswith("//"):
            raise ValueError("OIDC_ISSUER may end with at most one slash")
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

    def companion_chat(self) -> bool:
        """Whether readers can talk with Mothli on this server: the streamed, checked chat inside a
        book and on the home screen, with the page context and read map it needs. Hosted mode tells
        the app at sign-in (``GET /api/auth/session``), so it shows no chat where there is none."""
        return bool(self.reader_conversation_stream and self.reader_page_context
                    and self.reader_read_map)

    def paddle_prices(self) -> frozenset[str]:
        """Mothli's own Paddle price ids, monthly and yearly, old and current: a message about
        any other price is not Mothli's (ADR 0045 step 3)."""
        return frozenset(_items(self.paddle_price_monthly) + _items(self.paddle_price_yearly))

    def operator_published(self) -> bool:
        """Whether all three of the operator's details are set, so the public pages can be
        published in full (ADR 0045 step 8)."""
        return bool(self.operator_legal_name and self.operator_country
                    and self.operator_contact_email)

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
