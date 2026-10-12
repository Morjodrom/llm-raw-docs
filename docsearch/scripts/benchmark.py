#!/usr/bin/env python3
"""Deterministic synthetic offline benchmark; never reports fabricated numbers."""
import argparse
from dataclasses import asdict
import importlib.metadata
import json
import math
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from docsearch import DocSearch


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--documents', type=int, default=1000)
    parser.add_argument('--rounds', type=int, default=30)
    parser.add_argument('--output', type=Path, default=BASE / 'artifacts' / 'benchmark.json')
    args = parser.parse_args()
    if args.documents <= 0 or args.rounds <= 0:
        parser.error('documents and rounds must be positive')
    # Exclusive, disposable run directory prevents overwriting unrelated files.
    import tempfile
    artifacts = BASE / 'artifacts'
    artifacts.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='benchmark-', dir=artifacts))
    root = work / 'corpus'
    root.mkdir()
    for i in range(args.documents):
        text = f'# Руководство {i:04d}\nВведение в работу сервиса service_{i}.\n\n'
        for section in range(8):
            text += f'## Подключение {section}\nОшибка подключения к endpoint_{i}_{section}. HTTPClient config.user_id.\n'
            text += ('Настройка таймаута и проверка конфигурации. Retry connection timeout.\n' * 8)
            text += '| key | value |\n|---|---|\n| timeout | 123 |\n```sh\n--db /data/search.sqlite\n```\n\n'
        (root / f'doc-{i:04d}.md').write_text(text, encoding='utf-8')
    corpus_bytes = sum(p.stat().st_size for p in root.iterdir())
    db = work / 'search.sqlite'
    initialized = time.perf_counter()
    engine = DocSearch(db)
    initialization_seconds = time.perf_counter() - initialized
    with engine:
        start = time.perf_counter()
        report = engine.index(root)
        index_seconds = time.perf_counter() - start
        assert report.indexed == args.documents and not report.failures, "Benchmark indexing failed"
        start = time.perf_counter()
        unchanged = engine.index(root)
        unchanged_seconds = time.perf_counter() - start
        assert unchanged.unchanged == args.documents and not unchanged.failures, "Unchanged-index verification failed"
        cases = [('ошибки подключения', 'plain', 'all'), ('HTTPClient config.user_id', 'plain', 'all'),
                 ('таймаутов конфигураций', 'plain', 'all'), ('unknown connection', 'plain', 'any'),
                 ('body:"retry connection"', 'advanced', 'all'), ('--db /data/search.sqlite', 'literal', 'all')]
        timings = []
        by_query = []
        for query, mode, match in cases:
            engine.search(query, mode=mode, match=match)  # warm-up
            samples = []
            for _ in range(args.rounds):
                start = time.perf_counter()
                results = engine.search(query, mode=mode, match=match)
                samples.append((time.perf_counter() - start) * 1000)
            timings.extend(samples)
            by_query.append({'query': query, 'mode': mode, 'match': match, 'results': len(results),
                             'p50_ms': statistics.median(samples), 'p95_ms': percentile(samples, .95)})
    value = {'documents': args.documents, 'corpus_bytes': corpus_bytes, 'sections': report.sections,
             'database_bytes': db.stat().st_size, 'initialization_seconds': initialization_seconds,
             'index_seconds': index_seconds, 'unchanged_index_seconds': unchanged_seconds,
             'index_report': asdict(report), 'unchanged_report': asdict(unchanged),
             'query_p50_ms': statistics.median(timings), 'query_p95_ms': percentile(timings, .95),
             'query_samples': len(timings), 'queries': by_query,
             'environment': {'python': sys.version, 'platform': platform.platform(), 'sqlite': sqlite3.sqlite_version,
                             'dependencies': {name: importlib.metadata.version(name) for name in
                                              ('markdown-it-py', 'pymorphy3', 'pymorphy3-dicts-ru')}},
             'work_directory': str(work), 'notes': 'Warmed, in-process queries; initialization measured separately; literal mode scans all sections.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
