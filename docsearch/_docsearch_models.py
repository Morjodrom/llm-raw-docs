"""Side-effect-free public value types and exceptions."""
from dataclasses import dataclass, field
import math
from typing import Any


class DocSearchError(Exception):
    """Base class for expected application errors."""


class InvalidQueryError(DocSearchError):
    pass


class FTS5UnavailableError(DocSearchError):
    pass


class LemmatizerInitializationError(DocSearchError):
    pass


class IncompatibleIndexError(DocSearchError):
    pass


class ConfigurationMismatchError(DocSearchError):
    pass


class SourceRootMismatchError(DocSearchError):
    pass


class SectionNotFoundError(DocSearchError):
    pass


class ReadOnlyError(DocSearchError):
    pass


@dataclass(frozen=True)
class SearchConfig:
    title_boost: float = 5.0
    heading_boost: float = 3.0
    body_boost: float = 1.0
    rrf_k: int = 60
    candidate_min: int = 100
    candidate_multiplier: int = 10
    excerpt_chars: int = 320
    lemma_cache_size: int = 50000
    processing_version: str = "1"
    exclude: tuple[str, ...] = (
        ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "build", "dist",
        "FULL_HEADING_INDEX.md", "TABLE_OF_CONTENTS.md", "artifacts",
    )

    def __post_init__(self):
        for name in ("title_boost", "heading_boost", "body_boost"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("rrf_k", "candidate_min", "candidate_multiplier", "excerpt_chars", "lemma_cache_size"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not self.processing_version:
            raise ValueError("processing_version must not be empty")


@dataclass(frozen=True)
class IndexFailure:
    relative_path: str
    error: str
    retained_stale: bool


@dataclass
class IndexReport:
    indexed: int = 0
    unchanged: int = 0
    metadata_updated: int = 0
    deleted: int = 0
    sections: int = 0
    stale_documents: int = 0
    failures: list[IndexFailure] = field(default_factory=list)
    skipped_symlinks: list[str] = field(default_factory=list)
    processing_rebuilt: bool = False
    rolled_back: bool = False


@dataclass(frozen=True)
class SearchResult:
    section_id: str
    document_id: str
    title: str
    heading_path: list[str]
    excerpt: str
    relative_path: str
    start_line: int
    end_line: int
    provenance: dict[str, Any]
    matched_representation: str
    parent_section_id: str | None
    score: float
    stale: bool


@dataclass(frozen=True)
class Section:
    section_id: str
    document_id: str
    title: str
    heading_path: list[str]
    parent_section_id: str | None
    relative_path: str
    start_line: int
    end_line: int
    original_markdown: str
    provenance: dict[str, Any]
    stale: bool
