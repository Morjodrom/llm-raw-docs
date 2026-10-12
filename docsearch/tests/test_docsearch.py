from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from docsearch import (
    DocSearch, SearchConfig, InvalidQueryError, ReadOnlyError, DocSearchError,
    SectionNotFoundError, SourceRootMismatchError, ConfigurationMismatchError,
    IncompatibleIndexError, FTS5UnavailableError, LemmatizerInitializationError,
)
from _docsearch_extract import Extractor
from _docsearch_store import check_fts

BASE = Path(__file__).resolve().parents[1]


def write(root, text, name="пример.md"):
    path = root / name
    path.write_bytes(text.encode())
    return path


def test_structure_exact_lines_and_hierarchy(corpus, engine):
    text = 'Введение\r\n\r\n# Родитель\r\nТекст\r\n\r\n## Ребёнок\r\n| A | B |\r\n|---|---|\r\n| 1 | 2 |\r\n```md\r\n# Not a heading\r\n```\r\n\r\nЗаголовок\r\n=========\r\nEnd\r\n'
    write(corpus, text)
    report = engine.index(corpus)
    assert report.indexed == 1 and report.sections == 4
    rows = engine._db.execute('SELECT section_id FROM sections ORDER BY start_line').fetchall()
    sections = [engine.read_section(r[0]) for r in rows]
    assert ''.join(s.original_markdown for s in sections) == text
    assert [(s.start_line, s.end_line) for s in sections] == [(1, 2), (3, 5), (6, 13), (14, 16)]
    assert sections[2].parent_section_id == sections[1].section_id
    assert sections[2].heading_path == ['Родитель', 'Ребёнок']
    assert sections[3].parent_section_id is None
    assert sections[2].relative_path == 'пример.md'


def test_nested_headings_empty_and_no_heading():
    extractor = Extractor()
    assert extractor.extract('', 'a')[0] == []
    sections, _ = extractor.extract('intro\n> # quoted\n- item\n  # nested\n', 'a')
    assert len(sections) == 1
    assert sections[0]['heading_path'] == []


def test_russian_and_identifier_preservation(corpus, engine):
    write(corpus, '# Сеть\nОшибка подключения. HTTPClient user_id v2 123 неизвестноесловоццц\n')
    engine.index(corpus)
    result = engine.search('ошибки подключения')[0]
    assert result.matched_representation == 'lemmatized'
    assert 'Ошибка подключения' in result.excerpt
    for query in ('HTTPClient', 'user_id', 'v2', '123', 'неизвестноесловоццц'):
        assert engine.search(query)
    assert engine._processor.lemma('httpclient') == 'httpclient'
    assert engine._processor.lemma('неизвестноесловоццц') == 'неизвестноесловоццц'


def test_explicit_broadening_and_dedup(corpus, engine):
    write(corpus, '# Network\nconnection error\n')
    engine.index(corpus)
    assert engine.search('connection unknown') == []
    assert len(engine.search('connection unknown', match='any')) == 1
    results = engine.search('connection')
    assert len(results) == 1 and results[0].matched_representation == 'both'
    assert results[0].score == pytest.approx(2 / 61)


@pytest.mark.parametrize('query', ['"', '" OR 1=1 --', 'connection (', 'connection: error'])
def test_plain_escaping(corpus, engine, query):
    write(corpus, '# Hello\nconnection error\n')
    engine.index(corpus)
    engine.search(query, mode='literal' if query == '"' else 'plain')
    assert engine._db.execute('SELECT COUNT(*) FROM documents').fetchone()[0] == 1


@pytest.mark.parametrize('query', ['"unclosed', '(', 'badcolumn:foo', 'foo AND', 'NEAR('])
def test_invalid_advanced(corpus, engine, query):
    write(corpus, '# Hello\nfoo\n')
    engine.index(corpus)
    with pytest.raises(InvalidQueryError):
        engine.search(query, mode='advanced')


def test_advanced_original_only(corpus, engine):
    write(corpus, '# Сеть\nОшибка подключения foo bar\n')
    engine.index(corpus)
    assert engine.search('ошибки', mode='advanced') == []
    assert engine.search('body:"foo bar"', mode='advanced')[0].matched_representation == 'original'
    assert engine.search('foo OR absent', mode='advanced')


def test_literal_punctuation_and_case(corpus, engine):
    text = '# CLI\n`--db /data/search.sqlite`\nconfig.user_id\n## Keys\nother\n'
    write(corpus, text)
    engine.index(corpus)
    assert engine.search('--db /data/search.sqlite', mode='literal')
    assert engine.search('config.user_id', mode='literal')
    assert not engine.search('CONFIG.user_id', mode='literal')
    assert engine.search('CLI / Keys', mode='literal')[0].heading_path == ['CLI', 'Keys']


def test_ranking_title_boost(corpus, engine):
    write(corpus, '# Needle\nbody unrelated\n', 'a.md')
    write(corpus, '# Other\nneedle unrelated\n', 'b.md')
    engine.index(corpus)
    assert engine.search('needle')[0].relative_path == 'a.md'
    assert len(engine.search('needle')) == 2


def test_incremental_and_ids(corpus, engine):
    path = write(corpus, '# A\nbody\n## B\nchild\n')
    assert engine.index(corpus).indexed == 1
    ids = [r[0] for r in engine._db.execute('SELECT section_id FROM sections ORDER BY start_line')]
    assert engine.index(corpus).unchanged == 1
    path.write_text('# A\nnew body\n## B\nchild\n')
    assert engine.index(corpus).indexed == 1
    assert [r[0] for r in engine._db.execute('SELECT section_id FROM sections ORDER BY start_line')] == ids
    path.write_text('# Changed\nnew body\n## B\nchild\n')
    engine.index(corpus)
    assert not set(ids) & {r[0] for r in engine._db.execute('SELECT section_id FROM sections')}
    path.unlink()
    assert engine.index(corpus).deleted == 1
    assert engine.search('body') == []


def test_duplicate_headings_deterministic():
    extractor = Extractor()
    a = extractor.extract('# A\none\n# A\ntwo\n', 'd')[0]
    b = extractor.extract('# A\nmodified\n# A\ntwo\n', 'd')[0]
    assert len({s['section_id'] for s in a}) == 2
    assert [s['section_id'] for s in a] == [s['section_id'] for s in b]


def test_metadata_update_and_title_fallback(corpus, engine, monkeypatch):
    write(corpus, '# Fallback\nbody\n')
    sidecar = corpus / 'пример.provenance.json'
    sidecar.write_text(json.dumps({'title': 'Custom', 'source': 'original.docx'}))
    engine.index(corpus)
    sidecar.write_text(json.dumps({'title': 'Second', 'source': 'original.docx'}))
    def forbidden(*args):
        raise AssertionError('Body transformation must be skipped')
    original = engine._processor.transformed
    monkeypatch.setattr(engine._processor, 'transformed', lambda text: original(text) if text == 'Second' else forbidden())
    assert engine.index(corpus).metadata_updated == 1
    assert engine.search('Second')[0].title == 'Second'
    monkeypatch.setattr(engine._processor, 'transformed', original)
    sidecar.unlink()
    engine.index(corpus)
    assert engine.search('Fallback')[0].title == 'Fallback'


def test_normalized_provenance(corpus, engine):
    (corpus / 'manifest.jsonl').write_text('')
    roots = {'schema_version': 1, 'roots': {'source': {'path': '/original'}}}
    (corpus / 'roots.json').write_text(json.dumps(roots))
    folder = corpus / 'documents' / 'stable'
    folder.mkdir(parents=True)
    write(folder, '# Name\nОшибка подключения\n', 'content.md')
    provenance = {'document_id': 'stable', 'title': 'Canonical', 'sources': [{'path': 'src.docx', 'root_id': 'source'}]}
    (folder / 'provenance.json').write_text(json.dumps(provenance))
    write(corpus, '# generated\nignore\n', 'TABLE_OF_CONTENTS.md')
    engine.index(corpus)
    result = engine.search('ошибки')[0]
    assert result.document_id == 'stable'
    assert result.title == 'Canonical'
    assert result.provenance == dict(provenance, roots=roots)
    assert result.start_line == 1 and result.relative_path == 'documents/stable/content.md'


def test_failures_retain_stale(corpus, engine):
    path = write(corpus, '# Network\nconnection\n')
    engine.index(corpus)
    path.write_bytes(b'\xff')
    report = engine.index(corpus)
    assert report.failures[0].retained_stale
    assert engine.search('connection')[0].stale
    path.write_text('# Network\nconnection\n')
    assert engine.index(corpus).unchanged == 1
    assert not engine.search('connection')[0].stale


def test_document_transaction_rollback(corpus, engine, monkeypatch):
    path = write(corpus, '# Network\nconnection\n')
    engine.index(corpus)
    path.write_text('# Changed\nother\n')
    import docsearch
    real_delete = docsearch.delete_document
    def fail_after_delete(connection, did):
        real_delete(connection, did)
        raise sqlite3.OperationalError('injected insert failure')
    monkeypatch.setattr(docsearch, 'delete_document', fail_after_delete)
    assert engine.index(corpus).failures
    assert engine.search('connection')[0].stale
    assert not engine.search('other')


def test_inaccessible_subtree_suppresses_deletion(corpus, engine, monkeypatch):
    subtree = corpus / 'sub'
    subtree.mkdir()
    write(subtree, '# Network\nconnection\n')
    engine.index(corpus)
    import docsearch
    def walk(root, followlinks, onerror):
        onerror(PermissionError(13, 'denied', str(subtree)))
        yield str(root), [], []
    monkeypatch.setattr(docsearch.os, 'walk', walk)
    report = engine.index(corpus)
    assert report.deleted == 0 and report.failures
    assert engine.search('connection')[0].stale


def test_symlinks_exclusions_and_root_binding(corpus, engine, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    write(outside, '# Outside\nsecret\n')
    (corpus / 'linked').symlink_to(outside, target_is_directory=True)
    (corpus / 'link.md').symlink_to(outside / 'пример.md')
    write(corpus, '# Generated\nsecret\n', 'FULL_HEADING_INDEX.md')
    write(corpus, '# Real\nvisible\n')
    report = engine.index(corpus)
    assert len(report.skipped_symlinks) == 2 and report.indexed == 1
    assert not engine.search('secret')
    with pytest.raises(SourceRootMismatchError):
        engine.index(outside)
    corpus.rename(tmp_path / 'moved')
    assert engine.index(tmp_path / 'moved', relocate=True).unchanged == 1


def test_processing_change_atomic_rebuild(corpus, engine):
    write(corpus, '# A\nОшибка подключения\n')
    engine.index(corpus)
    db = engine.db_path
    with DocSearch(db, SearchConfig(processing_version='2')) as changed:
        with pytest.raises(ConfigurationMismatchError):
            changed.search('ошибки')
        report = changed.index(corpus)
        assert report.processing_rebuilt and changed.search('ошибки')
    with pytest.raises(ConfigurationMismatchError):
        engine.search('ошибки')


def test_failed_processing_rebuild_keeps_previous_index(corpus, engine):
    path = write(corpus, '# A\nОшибка подключения\n')
    write(corpus, '# B\noriginal\n', 'b.md')
    engine.index(corpus)
    path.write_bytes(b'\xff')
    write(corpus, '# B\nchanged\n', 'b.md')
    with DocSearch(engine.db_path, SearchConfig(processing_version='2')) as changed:
        report = changed.index(corpus)
        assert report.rolled_back and not report.processing_rebuilt
        with pytest.raises(ConfigurationMismatchError):
            changed.search('changed')
    assert engine.search('original') and not engine.search('changed')


def test_readonly_cleanup_and_independent_connections(corpus, engine):
    write(corpus, '# A\ntext\n')
    engine.index(corpus)
    with DocSearch(engine.db_path, read_only=True) as other:
        assert other._connection is not engine._connection
        assert other.search('text')
        with pytest.raises(ReadOnlyError):
            other.index(corpus)
        with pytest.raises(sqlite3.OperationalError):
            other._db.execute('DELETE FROM documents')
        with pytest.raises(SectionNotFoundError):
            other.read_section('missing')
    other.close()
    with pytest.raises(DocSearchError):
        other.search('text')
    assert engine.search('text')


def test_schema_rejection(tmp_path):
    db = tmp_path / 'unknown.sqlite'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE metadata(key TEXT, value TEXT)')
        c.execute("INSERT INTO metadata VALUES ('schema_version','999')")
    with pytest.raises(IncompatibleIndexError):
        DocSearch(db)


def test_missing_fts5():
    class Missing:
        def execute(self, *args):
            raise sqlite3.OperationalError('no such module: fts5')
    with pytest.raises(FTS5UnavailableError):
        check_fts(Missing())


def test_missing_dictionaries(tmp_path, monkeypatch):
    import pymorphy3_dicts_ru
    monkeypatch.setattr(pymorphy3_dicts_ru, 'get_path', lambda: str(tmp_path / 'missing'))
    with pytest.raises(LemmatizerInitializationError):
        DocSearch(tmp_path / 'db.sqlite')
    assert not (tmp_path / 'db.sqlite').exists()


def run_cli(*args, cwd=None, env=None):
    return subprocess.run([sys.executable, str(BASE / 'cli.py'), *map(str, args)], cwd=cwd, env=env, capture_output=True, text=True)


def test_cli_api_external_application_parity(corpus, engine, tmp_path):
    write(corpus, '# A\nОшибка подключения\n')
    result = run_cli('index', corpus, '--db', engine.db_path, '--json', cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['indexed'] == 1
    result = run_cli('search', '--db', engine.db_path, 'ошибки подключения', '--json', cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    cli_results = json.loads(result.stdout)['results']
    assert cli_results == [asdict(r) for r in engine.search('ошибки подключения')]
    result = run_cli('read', '--db', engine.db_path, cli_results[0]['section_id'], '--json')
    assert json.loads(result.stdout) == asdict(engine.read_section(cli_results[0]['section_id']))
    # A genuinely separate app, with explicit local module-path integration.
    app = tmp_path / 'external.py'
    app.write_text(f"import sys\nsys.path.insert(0, {str(BASE)!r})\nfrom docsearch import DocSearch\nwith DocSearch({str(engine.db_path)!r}, read_only=True) as e:\n r=e.search('ошибки подключения')[0]\n assert 'Ошибка' in e.read_section(r.section_id).original_markdown\n print(r.section_id)\n")
    result = subprocess.run([sys.executable, str(app)], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == cli_results[0]['section_id']


def test_entrypoints_import_side_effects_and_cli_errors(tmp_path, corpus, engine):
    env = dict(os.environ, PYTHONPATH=str(BASE))
    result = subprocess.run([sys.executable, '-m', 'docsearch', '--help'], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0 and 'index' in result.stdout
    code = '''import sys, sqlite3, os, logging, socket
from pathlib import Path
before=(os.getcwd(), dict(os.environ), list(logging.getLogger().handlers))
def blocked(*a, **k): raise AssertionError("import side effect")
sqlite3.connect=blocked
Path.read_bytes=blocked
Path.read_text=blocked
os.walk=blocked
socket.create_connection=blocked
sys.argv=['app', '--unexpected']
import docsearch
assert 'pymorphy3' not in sys.modules and 'markdown_it' not in sys.modules
assert before==(os.getcwd(), dict(os.environ), list(logging.getLogger().handlers))
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout == '' and result.stderr == ''
    write(corpus, '# A\ntext\n')
    engine.index(corpus)
    invalid = run_cli('search', '--db', engine.db_path, '(', '--mode', 'advanced', '--json')
    assert invalid.returncode == 2 and invalid.stdout == '' and 'Traceback' not in invalid.stderr
    missing = run_cli('read', '--db', engine.db_path, 'missing')
    assert missing.returncode == 3 and 'Traceback' not in missing.stderr
    empty = run_cli('search', '--db', engine.db_path, 'nonexistent', '--json')
    assert empty.returncode == 0 and json.loads(empty.stdout)['results'] == []


def test_commonmark_line_endings_not_unicode_separators():
    from _docsearch_extract import source_lines
    text = '\ufeff# Heading\rbody\u2028same line\u0085still same\r## Child\rfinal'
    sections, title = Extractor().extract(text, 'unicode')
    assert title == 'Heading'
    assert [(s['start_line'], s['end_line']) for s in sections] == [(1, 2), (3, 4)]
    assert ''.join(s['original_markdown'] for s in sections) == text
    assert len(source_lines(text)) == 4


def test_fts_initialization_failure_closes_connection(tmp_path, monkeypatch):
    import _docsearch_store
    import docsearch
    original = docsearch.sqlite3.connect
    connections = []
    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        connections.append(connection)
        return connection
    def fail(connection):
        raise FTS5UnavailableError('simulated missing extension')
    monkeypatch.setattr(docsearch.sqlite3, 'connect', connect)
    monkeypatch.setattr(_docsearch_store, 'check_fts', fail)
    with pytest.raises(FTS5UnavailableError):
        DocSearch(tmp_path / 'db.sqlite')
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute('SELECT 1')


def test_invalid_sidecar_and_new_file_failure(corpus, engine):
    write(corpus, '# A\nold content\n')
    engine.index(corpus)
    (corpus / 'пример.provenance.json').write_text('{malformed')
    write(corpus, '# New\ncontent\n', 'new.md')
    (corpus / 'new.provenance.json').write_text('[]')
    report = engine.index(corpus)
    assert len(report.failures) == 2
    assert {f.relative_path: f.retained_stale for f in report.failures} == {'new.md': False, 'пример.md': True}
    assert engine.search('old')[0].stale


def test_ranking_changes_do_not_rebuild_processing(corpus, engine):
    write(corpus, '# A\ntext\n')
    engine.index(corpus)
    with DocSearch(engine.db_path, SearchConfig(title_boost=10), read_only=True) as other:
        assert other.search('text')


@pytest.mark.parametrize('query,kwargs', [('', {}), (' ', {}), ('!!!', {}), ('x', {'limit':0}), ('x', {'limit':True}), ('x', {'mode':'bad'})])
def test_invalid_query_parameters(corpus, engine, query, kwargs):
    write(corpus, '# A\nx\n')
    engine.index(corpus)
    with pytest.raises(InvalidQueryError):
        engine.search(query, **kwargs)


def test_cli_partial_index_exit(corpus, engine):
    path = write(corpus, '# A\nx\n')
    engine.index(corpus)
    path.write_bytes(b'\xff')
    result = run_cli('index', corpus, '--db', engine.db_path, '--json')
    assert result.returncode == 1
    assert json.loads(result.stdout)['failures'][0]['retained_stale']
    assert 'retained_stale=True' in result.stderr and 'Traceback' not in result.stderr


def test_interrupted_initial_index_keeps_processing_binding(corpus, engine, monkeypatch):
    write(corpus, '# A\nfirst\n', 'a.md')
    write(corpus, '# B\nsecond\n', 'b.md')
    original = engine._document
    def interrupt(path, *args):
        if path.name == 'b.md':
            raise KeyboardInterrupt()
        return original(path, *args)
    monkeypatch.setattr(engine, '_document', interrupt)
    with pytest.raises(KeyboardInterrupt):
        engine.index(corpus)
    assert engine.search('first')
    with DocSearch(engine.db_path, SearchConfig(processing_version='2')) as changed:
        assert changed.index(corpus).processing_rebuilt
        assert changed.search('second')
