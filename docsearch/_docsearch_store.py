"""SQLite schema and explicit availability/version checks."""
import sqlite3
from _docsearch_models import FTS5UnavailableError, IncompatibleIndexError

SCHEMA_VERSION = "1"
SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE documents (
 document_id TEXT PRIMARY KEY, relative_path TEXT UNIQUE NOT NULL,
 title TEXT NOT NULL, content_hash TEXT NOT NULL, provenance_hash TEXT NOT NULL,
 provenance TEXT NOT NULL, stale INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE sections (
 rowid INTEGER PRIMARY KEY, section_id TEXT UNIQUE NOT NULL,
 document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
 heading_path TEXT NOT NULL, parent_section_id TEXT,
 start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, original_markdown TEXT NOT NULL
);
CREATE INDEX sections_document ON sections(document_id);
CREATE VIRTUAL TABLE original_fts USING fts5(title, heading, body, tokenize='unicode61 remove_diacritics 0');
CREATE VIRTUAL TABLE lemma_fts USING fts5(title_lemmas, heading_lemmas, body_lemmas, tokenize='unicode61 remove_diacritics 0');
"""


def check_fts(connection):
    try:
        connection.execute("CREATE VIRTUAL TABLE temp.docsearch_probe USING fts5(text, tokenize='unicode61')")
        connection.execute("DROP TABLE temp.docsearch_probe")
    except sqlite3.DatabaseError as exc:
        raise FTS5UnavailableError(f"SQLite FTS5 is unavailable: {exc}") from exc


def initialize(connection, read_only):
    check_fts(connection)
    names = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not names and not read_only:
        connection.executescript(SCHEMA)
        connection.execute("INSERT INTO metadata VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        connection.commit()
    else:
        try:
            row = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        except sqlite3.DatabaseError as exc:
            raise IncompatibleIndexError("Not a docsearch index; use a new database") from exc
        if not row or row[0] != SCHEMA_VERSION:
            raise IncompatibleIndexError(f"Expected index schema {SCHEMA_VERSION}; found {row[0] if row else 'missing'}")
        if not {"documents", "sections", "original_fts", "lemma_fts"}.issubset(names):
            raise IncompatibleIndexError("Incomplete docsearch schema")


def get_meta(connection, key):
    row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(connection, key, value):
    connection.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def delete_document(connection, document_id):
    for table in ("original_fts", "lemma_fts"):
        connection.execute(f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM sections WHERE document_id=?)", (document_id,))
    connection.execute("DELETE FROM documents WHERE document_id=?", (document_id,))
