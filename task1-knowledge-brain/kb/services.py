"""Wires the components together (one place, used by API, CLI and evals)."""
from __future__ import annotations

from dataclasses import dataclass

from .answering.rag import QueryService
from .config import Settings, get_settings
from .db import Database
from .escalations import Escalations
from .ingestion.crawler import Fetcher
from .ingestion.pipeline import IngestionPipeline
from .llm import build_llm
from .retrieval.embeddings import get_embedder
from .retrieval.store import ChunkStore


@dataclass
class Services:
    settings: Settings
    db: Database
    store: ChunkStore
    llm: object
    escalations: Escalations
    query: QueryService
    pipeline: IngestionPipeline


def build_services(settings: Settings | None = None, llm=None, fetcher: Fetcher | None = None,
                   allow_embedder_mismatch: bool = False) -> Services:
    settings = settings or get_settings()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    store = ChunkStore(db, get_embedder(settings), allow_mismatch=allow_embedder_mismatch)
    llm = llm or build_llm(settings)
    esc = Escalations(db, store)
    return Services(
        settings=settings, db=db, store=store, llm=llm, escalations=esc,
        query=QueryService(settings, db, store, llm, esc),
        pipeline=IngestionPipeline(settings, db, store, llm, fetcher=fetcher),
    )
