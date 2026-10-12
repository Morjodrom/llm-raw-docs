from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FileRecord:
    path: str
    kind: str
    status: str = "pending"
    reason: str | None = None
    sha256: str | None = None
    output: str | None = None
    document_id: str | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class Fragment:
    text: str
    base: Path


@dataclass
class Converted:
    source: str
    title: str
    fragments: list[Fragment]
    converter: str
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    locators: dict = field(default_factory=dict)
    extraction_inputs: list[str] = field(default_factory=list)

    @property
    def text(self):
        return "".join(f.text for f in self.fragments)
