from functools import lru_cache
from typing import Self

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings
from stash_shared import secrets
from stash_shared.embeddings import DEFAULT_EMBEDDING_MODEL


class Settings(BaseSettings):
    # Where the service runs; picks the logging implementation (see
    # `stash_shared.log`): "local" = readable lines, "aws" = Powertools,
    # anything else (e.g. "digitalocean") = JSON lines.
    platform: str = "local"
    # Deployment stage (local, dev, stage, prod...). Only labels logs.
    environment: str = "local"
    log_level: str = "INFO"
    # Names this process in logs and traces.
    service_name: str = "api"
    # Distributed tracing (see `stash_shared.tracing`): spans are exported
    # over OTLP/HTTP to this collector (Jaeger locally).
    tracing_enabled: bool = False
    tracing_otlp_endpoint: str = "http://localhost:4318"
    # Metrics (see `stash_shared.metrics`): published to CloudWatch, under
    # this namespace, only when `platform` is "aws"; a no-op elsewhere.
    metrics_namespace: str = "Stash"
    database_url: str = "postgresql+asyncpg://stash:stash@localhost:5432/stash"
    # AWS: the Secrets Manager secret with the database credentials; when
    # set, `database_url` is built from it (see `stash_shared.secrets`).
    database_secret_arn: str = ""
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 14
    cors_allowed_origins: list[str] = ["http://localhost:8080", "http://127.0.0.1:8080"]
    # Unset: picked from `platform` (SQS on "aws", Valkey elsewhere); see
    # `stash_shared.queue.factory`.
    queue_provider: str | None = None
    valkey_url: str = "redis://localhost:6379"
    # SQS only: queue name (`stash_shared.queue.base`) -> queue URL, as JSON,
    # e.g. SQS_QUEUE_URLS='{"thumbnail_jobs": "https://sqs..."}'. Credentials
    # and region come from the execution role, not settings.
    sqs_queue_urls: dict[str, str] = {}
    s3_endpoint_url: str = "http://localhost:9000"
    # Used only for signing download URLs handed to the browser, which can't
    # resolve the `minio` Docker-network hostname `s3_endpoint_url` normally
    # holds locally. Defaults to matching `s3_endpoint_url` in environments
    # (e.g. production, on AWS S3) where there's no
    # internal/external split.
    s3_public_endpoint_url: str = "http://localhost:9000"
    # Set both endpoints and both keys to "" on AWS: S3 itself, with the
    # task role's credentials.
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "stash"
    # A pre-signed URL also stops working when the credentials that signed
    # it expire. With a task role's temporary credentials (boto3 only
    # refreshes them shortly before they expire) that can be well before
    # this TTL, so keep it short there and let clients re-fetch the URL.
    image_download_url_ttl_seconds: int = 3600
    # A video's or audio file's playback URL (`GET /items/{id}/playback-url`),
    # fetched when the viewer opens, has to last a whole playback session:
    # the player keeps making range requests with it for as long as it's
    # played and sought. The same credential caveat applies, so clients
    # fetch a new URL (and resume where they were) when playback fails
    # with it.
    playback_url_ttl_seconds: int = 4 * 3600
    # How long a pre-signed upload URL can be used to *start* an upload
    # (S3 checks it when the request arrives, so a slow upload may finish
    # later). Also stamped on the pending upload as `expires_at`.
    upload_url_ttl_seconds: int = 900
    # Search queries are embedded with this; it must be the model the
    # embedding worker uses for item text.
    openai_api_key: str = ""
    # AWS: the Secrets Manager secret holding the OpenAI API key; when set,
    # it replaces `openai_api_key`. Read only when search first needs it
    # (`get_openai_api_key`), never at startup.
    openai_api_key_secret_arn: str = ""
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    # Search queries are first rewritten into English with this model (see
    # `app.query_normalization`); a small, fast one is enough. It must
    # accept `reasoning.effort = "minimal"` (the GPT-5 family does). If the
    # call fails or takes longer than the timeout, the original query is
    # searched instead.
    search_query_normalization_model: str = "gpt-5-nano"
    search_query_normalization_timeout_seconds: float = 5.0
    # Items whose best-matching search chunk is further than this cosine
    # distance from the query (0 = same direction, 1 = unrelated, 2 =
    # opposite) are left out, so a search doesn't return the whole library
    # ranked; None returns everything. Measured with text-embedding-3-small,
    # short queries against an image's short chunks: real matches scored
    # 0.24-0.53 ("girl" -> "young woman, girl, female" 0.41), unrelated
    # queries 0.646+ ("man" 0.646, "kitten" 0.695 against that same chunk).
    # 0.7, tuned for whole-description embeddings, let "kitten" through.
    # Borderline literal matches ("city" 0.66) are found by the full-text
    # match instead. Retune if the model or the chunk prompt changes.
    search_max_cosine_distance: float | None = 0.6
    # The user's own text (notes, links, captions) matches a search when its
    # closest stretch shares at least this share of the query's trigrams
    # (pg_trgm `word_similarity`, 0-1): 0.6 lets "город" find "городу"
    # (0.83) and "city" find "cities" (0.6), but not unrelated words.
    search_min_text_similarity: float = 0.6
    # TEMPORARY search diagnostics (`Semantic search candidate` log lines):
    # also log each candidate's best-matching chunk text. That's user
    # content (a note, a caption, text read from an image), so it's off by
    # default and refused anywhere but `environment=local`.
    search_log_chunk_text: bool = False

    # Largest upload, checked on the declared size (the upload URL is
    # signed for exactly that size, so storage enforces it too) and again
    # on what arrived. The bytes go straight to S3, never through the API,
    # which reads back only a few KB to classify them. How much of a file
    # the workers process is limited separately (their own settings).
    max_image_upload_bytes: int = 100 * 1024 * 1024
    max_file_upload_bytes: int = 500 * 1024 * 1024

    # Largest request bodies the API reads (JSON only: file bytes go
    # straight to storage), per endpoint category (`app.body_size`), 413
    # over them. Sized from the longest valid request of each, in the worst
    # case: a JSON string character takes at most 12 bytes, as a
    # `\uXXXX\uXXXX` surrogate pair (an emoji, from a client that escapes
    # non-ASCII, like Python's json.dumps); 4 as raw UTF-8, 6 as a `\u00XX`
    # escape. Field lengths are counted in characters (code points).
    #
    # Content (`POST /items/text`, `POST /uploads`, `PATCH /items/{id}`):
    # the largest is `POST /uploads`: caption 100,000 x 12 = 1,200,000,
    # filename 1,000 x 12 = 12,000, content type 255 x 12 = 3,060, 20 tags
    # x 200 x 12 = 48,000, the rest (keys, quotes, commas, type, size)
    # under 500: at most 1,263,560 bytes (1.2 MiB). 2 MiB leaves 66% more
    # for whitespace and other encoders; raw UTF-8 needs at most 450 KB.
    max_content_request_body_bytes: int = 2 * 1024 * 1024
    # Everything else. The largest is `POST /users`: email 254 x 12 =
    # 3,048, password 72 x 12 = 864, Turnstile token 2,048 x 12 = 24,576,
    # plus keys: under 29 KB; then `POST /items/delete` (100 ids of 36
    # characters, each `\u00XX`-escaped: 21,910) and `POST /search` (query
    # 1,000 x 12 plus filters: about 17 KB). 64 KiB is over twice the
    # largest.
    max_request_body_bytes: int = 64 * 1024

    # Rate limits and quotas (see `app.rate_limits`), counted in Postgres so
    # they hold across API instances. Each is a comma-separated list of
    # windows, "<count>/<window>" with the window in s, m, h or d; a request
    # must fit in every window. "" means unlimited. `rate_limits_enabled`
    # turns all of them off (e.g. for load tests), never partially.
    rate_limits_enabled: bool = True
    # Every login attempt from one IP, whatever its outcome.
    login_limit_per_ip: str = "20/1m,100/1h,500/1d"
    # Failed logins for one email from one IP: a user mistyping, or one
    # attacker guessing from one address. The strict one: 5 tries, then a
    # wait of at most 5 minutes; 10 in half an hour at most.
    login_failure_limit_per_account_ip: str = "5/5m,10/30m"
    # Failed logins for one email from anywhere: guessing spread over many
    # IPs (at most 50 guesses an hour, 1,200 a day). Looser than per IP,
    # and short: whoever knows an email can use it to block that account's
    # logins (from every IP, the owner's too), but for 15 minutes at a
    # time, an hour at most, never a day, and only by keeping it up, each
    # attempt also counting against their own IPs. No window is longer
    # than an hour, so no login is blocked for longer.
    login_failure_limit_per_account: str = "20/15m,50/1h"
    registration_limit_per_ip: str = "5/1h,20/1d"
    # Registration attempts naming one email (also slows probing which
    # emails are registered).
    registration_limit_per_email: str = "5/1h"

    # Cloudflare Turnstile on registration (`app.turnstile`): per-user
    # quotas only hold if accounts aren't free to mass-create. Every
    # registration must carry a token the client got from the Turnstile
    # widget, verified with Cloudflare (siteverify) before the account is
    # created. False only where there's no widget (local development
    # without internet, tests). Locally, Cloudflare's test keys pass every
    # time (see .env.example).
    turnstile_enabled: bool = True
    turnstile_secret_key: str = ""
    # AWS: the Secrets Manager secret holding the secret key; replaces
    # `turnstile_secret_key` when set. Read on the first registration.
    turnstile_secret_key_secret_arn: str = ""
    # Hostnames a token may have been solved on (Cloudflare reports it):
    # the frontend's. Empty: any (local development, test keys).
    turnstile_allowed_hostnames: list[str] = []
    turnstile_verify_url: str = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    turnstile_timeout_seconds: float = 5.0
    token_refresh_limit_per_ip: str = "60/5m,1000/1d"
    # Wrong current passwords on `POST /users/me/password`: a stolen access
    # token must not become a password-guessing oracle.
    password_change_failure_limit_per_user: str = "5/15m,20/1d"
    # Uploads started (`POST /uploads`), whether or not they're finalized.
    upload_limit_per_user: str = "100/5m,1000/1d"
    # Bytes of uploads started, by their declared (signed) size.
    upload_bytes_quota_per_user: str = f"{5 * 1024**3}/1d"
    # Uploads that will be analyzed by OpenAI (every image, analyzable
    # documents), charged when the upload starts.
    ai_analysis_quota_per_user: str = "300/1h,1000/1d"
    # Searches: each one is an LLM rewrite plus an embedding. The client
    # searches as the user types (debounced), hence the generous burst.
    search_limit_per_user: str = "60/1m,3000/1d"
    # Notes/links created and items edited: each may queue an embedding.
    item_write_limit_per_user: str = "120/5m,3000/1d"

    @field_validator(
        "login_limit_per_ip",
        "login_failure_limit_per_account_ip",
        "login_failure_limit_per_account",
        "registration_limit_per_ip",
        "registration_limit_per_email",
        "token_refresh_limit_per_ip",
        "password_change_failure_limit_per_user",
        "upload_limit_per_user",
        "upload_bytes_quota_per_user",
        "ai_analysis_quota_per_user",
        "search_limit_per_user",
        "item_write_limit_per_user",
    )
    @classmethod
    def _valid_limit(cls, value: str) -> str:
        # Fail at startup, not on the first request that needs the limit.
        from app.rate_limits.windows import parse_windows

        parse_windows(value)
        return value

    @model_validator(mode="after")
    def _content_logging_only_locally(self) -> Self:
        if self.search_log_chunk_text and self.environment.lower() != "local":
            raise ValueError("SEARCH_LOG_CHUNK_TEXT logs user content: only allowed with ENVIRONMENT=local")
        return self

    @model_validator(mode="after")
    def _resolve_secrets(self) -> Self:
        # Only the database secret, which every route needs. The OpenAI key
        # is search's alone: see `get_openai_api_key`.
        secrets.resolve_database_url(self)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_turnstile_secret_key() -> str:
    """The Turnstile secret key, from its secret on AWS
    (`turnstile_secret_key_secret_arn`), fetched on first use like the
    OpenAI key."""
    settings = get_settings()
    if settings.turnstile_secret_key_secret_arn:
        return secrets.get_secret_string(settings.turnstile_secret_key_secret_arn)
    return settings.turnstile_secret_key


def get_openai_api_key() -> str:
    """The OpenAI key, from its secret on AWS (`openai_api_key_secret_arn`),
    fetched on first use: the API starts, and serves everything but search,
    without it. Raises if the secret can't be read (e.g. no value set yet);
    only a successful read is cached, so a later search tries again."""
    return secrets.openai_api_key(get_settings())
