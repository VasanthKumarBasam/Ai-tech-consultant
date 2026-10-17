"""Runtime configuration for the Knowledge Brain.

Everything is read from environment variables (optionally via a .env file at the
repo root) so the same code runs in tests, locally, and on a schedule.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional: load ../.env when present
    from dotenv import load_dotenv

    _ROOT = Path(__file__).resolve().parents[2]
    load_dotenv(_ROOT / ".env")
except Exception:  # pragma: no cover - dotenv is optional
    pass

TASK_DIR = Path(__file__).resolve().parents[1]

# USD per million tokens (input, output). Verify against
# https://platform.claude.com/docs/en/models/overview before reporting costs;
# override with MODEL_PRICING_JSON='{"model-id": [in, out]}'.
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),  # docs list this as a "from" price
    "fake": (0.0, 0.0),
}


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _pricing() -> dict[str, tuple[float, float]]:
    pricing = dict(DEFAULT_PRICING)
    raw = os.getenv("MODEL_PRICING_JSON")
    if raw:
        for k, v in json.loads(raw).items():
            pricing[k] = (float(v[0]), float(v[1]))
    return pricing


@dataclass
class Settings:
    # storage
    db_path: Path = field(default_factory=lambda: Path(os.getenv("KB_DB_PATH", TASK_DIR / "data" / "kb.sqlite3")))

    # source
    sitemap_url: str = os.getenv(
        "KB_SITEMAP_URL", "https://support.brilliantdirectories.com/support/sitemap.xml"
    )
    article_url_pattern: str = os.getenv("KB_ARTICLE_URL_PATTERN", r"/support/solutions/articles/\d+")
    user_agent: str = os.getenv(
        "KB_USER_AGENT", "AITechConsultant-KnowledgeBrain/1.0 (capstone project; polite crawler)"
    )

    # crawl politeness
    crawl_concurrency: int = int(os.getenv("KB_CRAWL_CONCURRENCY", "2"))
    crawl_delay_s: float = float(os.getenv("KB_CRAWL_DELAY_SECONDS", "1.0"))
    image_fetch_delay_s: float = float(os.getenv("KB_IMAGE_FETCH_DELAY_SECONDS", "0.25"))
    request_timeout_s: float = float(os.getenv("KB_REQUEST_TIMEOUT_SECONDS", "20"))
    max_retries: int = int(os.getenv("KB_MAX_RETRIES", "4"))
    backoff_base_s: float = float(os.getenv("KB_BACKOFF_BASE_SECONDS", "1.5"))
    max_page_attempts: int = int(os.getenv("KB_MAX_PAGE_ATTEMPTS", "5"))
    respect_robots: bool = _bool("KB_RESPECT_ROBOTS", True)
    # refuse to deactivate more than this share of pages in one run (broken sitemap guard)
    max_removal_ratio: float = float(os.getenv("KB_MAX_REMOVAL_RATIO", "0.3"))

    # images
    images_enabled: bool = _bool("KB_IMAGES_ENABLED", True)
    image_max_per_article: int = int(os.getenv("KB_IMAGE_MAX_PER_ARTICLE", "12"))
    image_min_bytes: int = int(os.getenv("KB_IMAGE_MIN_BYTES", "4000"))
    image_max_bytes: int = int(os.getenv("KB_IMAGE_MAX_BYTES", str(4_500_000)))

    # chunking
    chunk_strategy: str = os.getenv("KB_CHUNK_STRATEGY", "heading")  # heading | fixed
    chunk_max_chars: int = int(os.getenv("KB_CHUNK_MAX_CHARS", "1800"))
    chunk_min_chars: int = int(os.getenv("KB_CHUNK_MIN_CHARS", "250"))
    chunk_overlap_chars: int = int(os.getenv("KB_CHUNK_OVERLAP_CHARS", "200"))

    # embeddings: fastembed (local, default) | voyage | hash (offline/test fallback)
    embedder: str = os.getenv("KB_EMBEDDER", "fastembed")
    fastembed_model: str = os.getenv("KB_FASTEMBED_MODEL", "BAAI/bge-small-en-v1.5")
    voyage_model: str = os.getenv("KB_VOYAGE_MODEL", "voyage-3.5-lite")

    # retrieval
    retrieval_mode: str = os.getenv("KB_RETRIEVAL_MODE", "hybrid")  # vector | keyword | hybrid
    top_k: int = int(os.getenv("KB_TOP_K", "8"))
    candidate_k: int = int(os.getenv("KB_CANDIDATE_K", "30"))
    # below this top cosine similarity we abstain without calling the LLM.
    # None -> embedder-specific default (see retrieval.search.default_min_similarity)
    min_similarity: float | None = (
        float(os.environ["KB_MIN_SIMILARITY"]) if os.getenv("KB_MIN_SIMILARITY") else None
    )

    # LLM
    llm_provider: str = os.getenv("LLM_PROVIDER", "anthropic")  # anthropic | fake
    answer_model: str = os.getenv("KB_ANSWER_MODEL", "claude-sonnet-5-5")
    vision_model: str = os.getenv("KB_VISION_MODEL", "claude-haiku-5-5")
    llm_timeout_s: float = float(os.getenv("KB_LLM_TIMEOUT_SECONDS", "60"))
    answer_max_tokens: int = int(os.getenv("KB_ANSWER_MAX_TOKENS", "1200"))
    pricing: dict[str, tuple[float, float]] = field(default_factory=_pricing)

    # API
    api_key: str | None = os.getenv("KB_API_KEY") or None  # if set, clients must send X-API-Key
    daily_schedule_cron: str | None = os.getenv("KB_DAILY_CRON", "0 2 * * *") or None
    scheduler_enabled: bool = _bool("KB_SCHEDULER_ENABLED", False)

    def __post_init__(self) -> None:
        self.db_path = Path(self.db_path)

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings


def set_settings(s: Settings) -> None:
    """Used by tests to inject an isolated configuration."""
    global _settings
    _settings = s
    s.ensure_dirs()
