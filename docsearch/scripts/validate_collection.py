#!/usr/bin/env python3
"""Validate real collection and external CLI/API integration without editing sources."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from docsearch import DocSearch
from _docsearch_extract import source_lines


def blocked(*args, **kwargs):
    raise AssertionError('Runtime networking is forbidden')


def snapshot(root):
    result = {}
    import os
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    artifacts = BASE / 'artifacts'
    artifacts.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='validation-', dir=artifacts))
    db = work / 'collection.sqlite'
    before = snapshot(root)
    socket.create_connection = blocked
    socket.socket.connect = blocked
    socket.getaddrinfo = blocked
    with DocSearch(db) as engine:
        start = time.perf_counter()
        report = engine.index(root)
        index_seconds = time.perf_counter() - start
        rerun = engine.index(root)
        results = engine.search('ошибки подключения')
        if not results:
            raise AssertionError('Acceptance query returned no results on real collection')
        section = engine.read_section(results[0].section_id)
        indexed_lines = {}
        for row in engine._db.execute('SELECT section_id FROM sections'):
            s = engine.read_section(row[0])
            if s.relative_path not in indexed_lines:
                indexed_lines[s.relative_path] = source_lines((root / s.relative_path).read_bytes().decode('utf-8'))
            lines = indexed_lines[s.relative_path]
            assert ''.join(lines[s.start_line - 1:s.end_line]) == s.original_markdown
    # Run outside source directory. sitecustomize blocks network in child interpreters too.
    (work / 'sitecustomize.py').write_text('import socket\ndef blocked(*a, **k): raise AssertionError("network forbidden")\nsocket.socket.connect=blocked\nsocket.create_connection=blocked\nsocket.getaddrinfo=blocked\n')
    import os
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(work), str(BASE))))
    def cli(command, *arguments):
        result = subprocess.run([sys.executable, str(BASE / 'cli.py'), command, '--db', str(db), '--json', *arguments],
                                cwd=work, env=env, text=True, capture_output=True, check=True)
        return json.loads(result.stdout)
    cli_index = cli('index', str(root))
    cli_search = cli('search', 'ошибки подключения')
    cli_read = cli('read', results[0].section_id)
    assert cli_search['results'] == [asdict(r) for r in results]
    assert cli_read == asdict(section)
    module = subprocess.run([sys.executable, '-m', 'docsearch', 'search', '--db', str(db), '--json', 'ошибки подключения'],
                            cwd=work, env=env, text=True, capture_output=True, check=True)
    assert json.loads(module.stdout) == cli_search
    app = work / 'external_app.py'
    app.write_text(f'''import sys, json
from dataclasses import asdict
sys.path.insert(0, {str(BASE)!r})
from docsearch import DocSearch, SearchConfig
with DocSearch({str(db)!r}, config=SearchConfig()) as e:
 report=e.index({str(root)!r})
 results=e.search("ошибки подключения")
 section=e.read_section(results[0].section_id)
 print(json.dumps({{"report":asdict(report),"results":[asdict(r) for r in results],"section":asdict(section)}}, ensure_ascii=False))
''')
    external = subprocess.run([sys.executable, str(app)], cwd=work, env=env, text=True, capture_output=True, check=True)
    external_data = json.loads(external.stdout)
    assert external_data['results'] == cli_search['results'] and external_data['section'] == cli_read
    assert before == snapshot(root), 'Source collection changed during validation'
    value = {'source_root': str(root), 'source_files_unchanged': len(before), 'index_report': asdict(report),
             'rerun_report': asdict(rerun), 'index_seconds': index_seconds,
             'acceptance_query': 'ошибки подключения', 'result_count': len(results),
             'first_result': asdict(results[0]), 'cli_index_report': cli_index,
             'cli_api_parity': True, 'module_entrypoint_parity': True, 'external_app_index_search_read': True,
             'all_section_line_ranges_verified': report.sections, 'runtime_network_blocked': True,
             'work_directory': str(work)}
    (artifacts / 'validation.json').write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in value.items() if k != 'first_result'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
