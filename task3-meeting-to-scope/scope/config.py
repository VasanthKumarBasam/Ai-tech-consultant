"""Configuration (environment variables, optionally from ../.env)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # never under pytest, so tests don't depend on local settings
    import sys

    from dotenv import load_dotenv

    if "pytest" not in sys.modules:
        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:  # pragma: no cover
    pass

TASK_DIR = Path(__file__).resolve().parents[1]

DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "fake": (0.0, 0.0),
}


def _pricing() -> dict[str, tuple[float, float]]:
    p = dict(DEFAULT_PRICING)
    if os.getenv("MODEL_PRICING_JSON"):
        p.update({k: (float(v[0]), float(v[1])) for k, v in json.loads(os.environ["MODEL_PRICING_JSON"]).items()})
    return p


@dataclass
class Settings:
    db_path: Path = field(default_factory=lambda: Path(os.getenv("SCOPE_DB_PATH", TASK_DIR / "data" / "scope.sqlite3")))
    sent_dir: Path = field(default_factory=lambda: Path(os.getenv("SCOPE_SENT_DIR", TASK_DIR / "data" / "sent")))
    kb_url: str = field(default_factory=lambda: os.getenv("KB_URL", "http://127.0.0.1:8001").rstrip("/"))
    kb_api_key: str | None = field(default_factory=lambda: os.getenv("KB_API_KEY") or None)
    kb_timeout_s: float = field(default_factory=lambda: float(os.getenv("SCOPE_KB_TIMEOUT_SECONDS", "60")))
    kb_concurrency: int = field(default_factory=lambda: int(os.getenv("SCOPE_KB_CONCURRENCY", "3")))

    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "anthropic"))
    # one model per stage so evals can compare them independently
    model_extract: str = field(default_factory=lambda: os.getenv("SCOPE_MODEL_EXTRACT", "claude-sonnet-5-5"))
    model_merge: str = field(default_factory=lambda: os.getenv("SCOPE_MODEL_MERGE", "claude-sonnet-5-5"))
    model_feasibility: str = field(default_factory=lambda: os.getenv("SCOPE_MODEL_FEASIBILITY", "claude-sonnet-5-5"))
    model_scope: str = field(default_factory=lambda: os.getenv("SCOPE_MODEL_SCOPE", "claude-sonnet-5-5"))
    model_estimate: str = field(default_factory=lambda: os.getenv("SCOPE_MODEL_ESTIMATE", "claude-sonnet-5-5"))
    model_proposal: str = field(default_factory=lambda: os.getenv("SCOPE_MODEL_PROPOSAL", "claude-sonnet-5-5"))
    model_review: str = field(default_factory=lambda: os.getenv("SCOPE_MODEL_REVIEW", "claude-haiku-5-5"))
    model_change: str = field(default_factory=lambda: os.getenv("SCOPE_MODEL_CHANGE", "claude-sonnet-5-5"))
    llm_timeout_s: float = field(default_factory=lambda: float(os.getenv("SCOPE_LLM_TIMEOUT_SECONDS", "120")))

    # long-transcript handling
    chunk_max_chars: int = field(default_factory=lambda: int(os.getenv("SCOPE_CHUNK_MAX_CHARS", "6000")))
    chunk_overlap_lines: int = field(default_factory=lambda: int(os.getenv("SCOPE_CHUNK_OVERLAP_LINES", "3")))

    # commercial settings (indicative pricing only; a person approves every proposal)
    company_name: str = field(default_factory=lambda: os.getenv("COMPANY_NAME", "Northstar Web Studio"))
    hourly_rate: float = field(default_factory=lambda: float(os.getenv("SCOPE_HOURLY_RATE", "95")))
    currency: str = field(default_factory=lambda: os.getenv("SCOPE_CURRENCY", "USD"))
    base_contingency_pct: float = field(default_factory=lambda: float(os.getenv("SCOPE_BASE_CONTINGENCY_PCT", "10")))
    contingency_per_unclear_pct: float = field(default_factory=lambda: float(os.getenv("SCOPE_CONTINGENCY_PER_UNCLEAR_PCT", "3")))
    max_contingency_pct: float = field(default_factory=lambda: float(os.getenv("SCOPE_MAX_CONTINGENCY_PCT", "30")))
    max_task_hours: float = field(default_factory=lambda: float(os.getenv("SCOPE_MAX_TASK_HOURS", "40")))

    pricing: dict[str, tuple[float, float]] = field(default_factory=_pricing)

    def __post_init__(self) -> None:
        self.db_path = Path(self.db_path)
        self.sent_dir = Path(self.sent_dir)

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.sent_dir.mkdir(parents=True, exist_ok=True)

    def models(self) -> dict[str, str]:
        return {k[6:]: v for k, v in self.__dict__.items() if k.startswith("model_")}
