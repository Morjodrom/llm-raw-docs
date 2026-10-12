"""Local offline search API. Import does not initialize any resources."""
from dataclasses import asdict
from pathlib import Path
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import sys

from _docsearch_models import (
    SearchConfig, IndexReport, IndexFailure, SearchResult, Section, DocSearchError,
    InvalidQueryError, FTS5UnavailableError, LemmatizerInitializationError,
    IncompatibleIndexError, ConfigurationMismatchError, SourceRootMismatchError,
    SectionNotFoundError, ReadOnlyError,
)
from _docsearch_extract import Extractor, stable_id
from _docsearch_text import TextProcessor, TOKEN, words
from _docsearch_store import initialize, get_meta, set_meta, delete_document

__all__ = [
    "DocSearch", "SearchConfig", "IndexReport", "IndexFailure", "SearchResult", "Section",
    "DocSearchError", "InvalidQueryError", "FTS5UnavailableError", "LemmatizerInitializationError",
    "IncompatibleIndexError", "ConfigurationMismatchError", "SourceRootMismatchError",
    "SectionNotFoundError", "ReadOnlyError",
]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _hash(data):
    return hashlib.sha256(data).hexdigest()


class DocSearch:
    """Owns one thread-affine SQLite connection; call close or use a context manager."""
    def __init__(self, db_path, config=None, *, read_only=False):
        if not (3, 11) <= sys.version_info[:2] < (3, 15):
            raise DocSearchError("Requires Python >=3.11,<3.15")
        self.config = config or SearchConfig()
        self.db_path = Path(db_path).expanduser().absolute()
        self.read_only = read_only
        self._connection = None
        self._processor = None
        try:
            self._processor = TextProcessor(self.config)
            self._extractor = Extractor()
            uri = self.db_path.as_uri() + ("?mode=ro" if read_only else "?mode=rwc")
            self._connection = sqlite3.connect(uri, uri=True, timeout=5)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys=ON")
            initialize(self._connection, read_only)
            self._fingerprint = _json(self._processor.fingerprint)
            if read_only:
                self._ensure_processing()
        except Exception:
            self.close()
            raise

    @property
    def _db(self):
        if self._connection is None:
            raise DocSearchError("Engine is closed")
        return self._connection

    def __enter__(self):
        self._db
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._processor is not None:
            self._processor.close()
            self._processor = None

    def _ensure_processing(self):
        fingerprint = get_meta(self._db, "processing")
        if fingerprint is None:
            raise ConfigurationMismatchError("Index has not been built; run index first")
        if fingerprint != self._fingerprint:
            raise ConfigurationMismatchError("Processing configuration differs; re-index with a writable instance")

    def _excluded(self, relative):
        return any(fnmatch.fnmatch(relative, p) or any(fnmatch.fnmatch(part, p) for part in Path(relative).parts)
                   for p in self.config.exclude)

    def _scan(self, root, normalized, report):
        paths = []
        unsafe = []
        def failed(exc):
            filename = Path(exc.filename or root)
            try:
                relative = filename.relative_to(root).as_posix()
            except ValueError:
                relative = "."
            unsafe.append(relative)
            report.failures.append(IndexFailure(relative, str(exc), True))
        scan_root = root / "documents" if normalized else root
        if scan_root.is_symlink():
            raise DocSearchError("Document directory must not be a symlink")
        if not scan_root.is_dir():
            raise DocSearchError(f"Missing document directory: {scan_root}")
        for directory, dirs, files in os.walk(scan_root, followlinks=False, onerror=failed):
            directory = Path(directory)
            retained = []
            for name in sorted(dirs):
                path = directory / name
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    report.skipped_symlinks.append(relative)
                elif not self._excluded(relative):
                    retained.append(name)
            dirs[:] = retained
            for name in sorted(files):
                path = directory / name
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    report.skipped_symlinks.append(relative)
                elif not self._excluded(relative) and path.suffix.lower() in {".md", ".markdown"}:
                    if not normalized or (name == "content.md" and path.parent.parent == scan_root):
                        paths.append(path)
        return sorted(paths), unsafe

    @staticmethod
    def _read_json(path):
        if path.is_symlink():
            raise ValueError(f"Metadata symlink is not allowed: {path.name}")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"Metadata must be an object: {path.name}")
        return value

    def _document(self, path, root, normalized, roots):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("File is a symlink or escapes source root")
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        relative = path.relative_to(root).as_posix()
        sidecar = path.with_name("provenance.json") if normalized else path.with_suffix(".provenance.json")
        provenance = self._read_json(sidecar) if sidecar.exists() or sidecar.is_symlink() else {}
        document_id = provenance.get("document_id") if normalized else stable_id(relative)
        if normalized and (not isinstance(document_id, str) or not document_id):
            raise ValueError("Normalized document requires a nonempty document_id")
        if roots:
            provenance = dict(provenance, roots=roots)
        return document_id, relative, text, _hash(raw), provenance, _hash(_json(provenance).encode())

    def index(self, root, *, relocate=False):
        try:
            return self._index(root, relocate=relocate)
        except BaseException:
            self._db.rollback()
            raise

    def _index(self, root, *, relocate=False):
        if self.read_only:
            raise ReadOnlyError("Cannot index using a read-only instance")
        supplied = Path(root).expanduser().absolute()
        if supplied.is_symlink():
            raise DocSearchError("Source root must not be a symlink")
        root = supplied.resolve(strict=True)
        if not root.is_dir():
            raise DocSearchError("Source root must be a directory")
        bound = get_meta(self._db, "source_root")
        if bound and bound != str(root) and not relocate:
            raise SourceRootMismatchError(f"Index belongs to {bound}; use relocate=True only for a moved collection")
        normalized = (root / "manifest.jsonl").is_file() and (root / "roots.json").is_file()
        roots = self._read_json(root / "roots.json") if normalized else {}
        report = IndexReport()
        paths, unsafe = self._scan(root, normalized, report)
        old = {r["relative_path"]: r for r in self._db.execute("SELECT * FROM documents")}
        report.failures = [IndexFailure(f.relative_path, f.error,
            any(f.relative_path == "." or p == f.relative_path or p.startswith(f.relative_path.rstrip("/") + "/") for p in old))
            for f in report.failures]
        seen = set()
        prior_processing = get_meta(self._db, "processing")
        rebuilding = (prior_processing is not None and prior_processing != self._fingerprint) or (prior_processing is None and bool(old))
        if prior_processing is None and not old:
            # Bind before per-document commits so an interrupted first run remains versioned.
            with self._db:
                set_meta(self._db, "source_root", str(root))
                set_meta(self._db, "processing", self._fingerprint)
        if rebuilding:
            self._db.execute("BEGIN IMMEDIATE")
        for path in paths:
            relative = path.relative_to(root).as_posix()
            seen.add(relative)
            previous = old.get(relative)
            try:
                did, relative, text, content_hash, provenance, provenance_hash = self._document(path, root, normalized, roots)
                existing = self._db.execute("SELECT relative_path FROM documents WHERE document_id=?", (did,)).fetchone()
                if existing and existing[0] != relative:
                    raise ValueError(f"Duplicate document_id also used by {existing[0]}")
                if previous and previous["document_id"] == did and previous["content_hash"] == content_hash and not rebuilding:
                    with self._transaction(rebuilding):
                        if previous["provenance_hash"] != provenance_hash:
                            title = provenance.get("title") or self._extractor.extract(text, did)[1] or path.stem
                            if not isinstance(title, str):
                                raise ValueError("Title must be a string")
                            self._db.execute("UPDATE documents SET title=?, provenance=?, provenance_hash=?, stale=0 WHERE document_id=?",
                                             (title, _json(provenance), provenance_hash, did))
                            self._db.execute("UPDATE original_fts SET title=? WHERE rowid IN (SELECT rowid FROM sections WHERE document_id=?)",
                                             (self._processor.original(title), did))
                            self._db.execute("UPDATE lemma_fts SET title_lemmas=? WHERE rowid IN (SELECT rowid FROM sections WHERE document_id=?)",
                                             (self._processor.transformed(title), did))
                            report.metadata_updated += 1
                        else:
                            self._db.execute("UPDATE documents SET stale=0 WHERE document_id=?", (did,))
                            report.unchanged += 1
                    continue
                sections, first_heading = self._extractor.extract(text, did)
                title = provenance.get("title") or first_heading or path.stem
                if not isinstance(title, str):
                    raise ValueError("Title must be a string")
                prepared = []
                for s in sections:
                    fields = (title, s["heading"], s["original_markdown"])
                    prepared.append((s, tuple(self._processor.original(f) for f in fields),
                                     tuple(self._processor.transformed(f) for f in fields)))
                with self._transaction(rebuilding):
                    if previous:
                        delete_document(self._db, previous["document_id"])
                    self._db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,0)",
                                     (did, relative, title, content_hash, provenance_hash, _json(provenance)))
                    for s, original, lemmas in prepared:
                        cursor = self._db.execute("INSERT INTO sections(section_id,document_id,heading_path,parent_section_id,start_line,end_line,original_markdown) VALUES (?,?,?,?,?,?,?)",
                                                  (s["section_id"], did, _json(s["heading_path"]), s["parent_section_id"], s["start_line"], s["end_line"], s["original_markdown"]))
                        self._db.execute("INSERT INTO original_fts(rowid,title,heading,body) VALUES (?,?,?,?)", (cursor.lastrowid, *original))
                        self._db.execute("INSERT INTO lemma_fts(rowid,title_lemmas,heading_lemmas,body_lemmas) VALUES (?,?,?,?)", (cursor.lastrowid, *lemmas))
                report.indexed += 1
            except (OSError, ValueError, sqlite3.DatabaseError) as exc:
                report.failures.append(IndexFailure(relative, str(exc), previous is not None))
                if previous:
                    self._db.execute("UPDATE documents SET stale=1 WHERE document_id=?", (previous["document_id"],))
                    if not rebuilding:
                        self._db.commit()
        if rebuilding and report.failures:
            self._db.rollback()
            report.indexed = report.metadata_updated = report.unchanged = 0
            report.rolled_back = True
            with self._db:
                # Every retained representation is stale relative to the requested rebuild.
                self._db.execute("UPDATE documents SET stale=1")
        else:
            with self._transaction(rebuilding):
                for relative, previous in old.items():
                    blocked = any(p == "." or relative == p or relative.startswith(p.rstrip("/") + "/") for p in unsafe)
                    if relative not in seen and not blocked:
                        delete_document(self._db, previous["document_id"])
                        report.deleted += 1
                    elif blocked:
                        self._db.execute("UPDATE documents SET stale=1 WHERE document_id=?", (previous["document_id"],))
                set_meta(self._db, "source_root", str(root))
                set_meta(self._db, "processing", self._fingerprint)
            if rebuilding:
                self._db.commit()
                report.processing_rebuilt = True
        report.sections = self._db.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
        report.stale_documents = self._db.execute("SELECT COUNT(*) FROM documents WHERE stale=1").fetchone()[0]
        return report

    def _transaction(self, rebuilding):
        from contextlib import contextmanager
        @contextmanager
        def transaction():
            if rebuilding:
                self._db.execute("SAVEPOINT document_change")
            try:
                yield
                if rebuilding:
                    self._db.execute("RELEASE document_change")
                else:
                    self._db.commit()
            except BaseException:
                if rebuilding:
                    self._db.execute("ROLLBACK TO document_change")
                    self._db.execute("RELEASE document_change")
                else:
                    self._db.rollback()
                raise
        return transaction()

    def _row(self, section_id):
        row = self._db.execute("SELECT s.*, d.title, d.relative_path, d.provenance, d.stale FROM sections s JOIN documents d USING(document_id) WHERE section_id=?", (section_id,)).fetchone()
        if row is None:
            raise SectionNotFoundError(f"Section not found: {section_id}")
        return row

    def read_section(self, section_id):
        row = self._row(section_id)
        return Section(row["section_id"], row["document_id"], row["title"], json.loads(row["heading_path"]),
                       row["parent_section_id"], row["relative_path"], row["start_line"], row["end_line"],
                       row["original_markdown"], json.loads(row["provenance"]), bool(row["stale"]))

    @staticmethod
    def _expression(tokens, match):
        return (" AND " if match == "all" else " OR ").join('"' + w.replace('"', '""') + '"' for w in dict.fromkeys(tokens))

    def _candidates(self, table, expression, limit):
        weights = (self.config.title_boost, self.config.heading_boost, self.config.body_boost)
        try:
            return self._db.execute(f"SELECT s.section_id FROM {table} JOIN sections s ON s.rowid={table}.rowid WHERE {table} MATCH ? ORDER BY bm25({table},?,?,?), s.section_id LIMIT ?",
                                    (expression, *weights, limit)).fetchall()
        except sqlite3.OperationalError as exc:
            if any(x in str(exc).lower() for x in ("fts5", "syntax", "no such column", "unterminated")):
                raise InvalidQueryError(f"Invalid FTS query: {exc}") from exc
            raise

    def _excerpt(self, markdown, query, mode):
        if mode == "literal":
            position = max(0, markdown.find(query))
        else:
            targets = set(self._processor.lemmas(query))
            position = 0
            for token in TOKEN.finditer(markdown):
                value = words(token.group())[0]
                if self._processor.lemma(value) in targets:
                    position = token.start()
                    break
        start = max(0, position - self.config.excerpt_chars // 4)
        end = min(len(markdown), start + self.config.excerpt_chars)
        return ("…" if start else "") + markdown[start:end] + ("…" if end < len(markdown) else "")

    def search(self, query, limit=10, *, mode="plain", match="all"):
        self._ensure_processing()
        if not isinstance(query, str) or not query.strip():
            raise InvalidQueryError("Query must be nonempty text")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise InvalidQueryError("limit must be an integer between 1 and 1000")
        if mode not in {"plain", "advanced", "literal"} or match not in {"all", "any"}:
            raise InvalidQueryError("mode must be plain/advanced/literal; match must be all/any")
        scores, representations = {}, {}
        if mode == "literal":
            rows = self._db.execute("SELECT section_id FROM sections ORDER BY section_id")
            # Verify heading text, not JSON escapes or separators.
            candidates = []
            for r in rows:
                row = self._row(r[0])
                if query in row["title"] or query in " / ".join(json.loads(row["heading_path"])) or query in row["original_markdown"]:
                    candidates.append(r)
            branches = [("original", candidates)]
        else:
            pool = max(self.config.candidate_min, self.config.candidate_multiplier * limit)
            if mode == "advanced":
                branches = [("original", self._candidates("original_fts", query, pool))]
            else:
                tokens = words(query)
                if not tokens:
                    raise InvalidQueryError("Plain query has no searchable words; use literal mode for punctuation")
                branches = [("original", self._candidates("original_fts", self._expression(tokens, match), pool)),
                            ("lemmatized", self._candidates("lemma_fts", self._expression(self._processor.lemmas(query), match), pool))]
        for representation, rows in branches:
            for rank, row in enumerate(rows, 1):
                sid = row[0]
                scores[sid] = scores.get(sid, 0) + 1 / (self.config.rrf_k + rank)
                representations.setdefault(sid, set()).add(representation)
        results = []
        for sid in sorted(scores, key=lambda sid: (-scores[sid], sid))[:limit]:
            row = self._row(sid)
            results.append(SearchResult(sid, row["document_id"], row["title"], json.loads(row["heading_path"]),
                self._excerpt(row["original_markdown"], query, mode), row["relative_path"], row["start_line"], row["end_line"],
                json.loads(row["provenance"]), "both" if len(representations[sid]) == 2 else next(iter(representations[sid])),
                row["parent_section_id"], scores[sid], bool(row["stale"])))
        return results


if __name__ == "__main__":
    from cli import main
    raise SystemExit(main())
