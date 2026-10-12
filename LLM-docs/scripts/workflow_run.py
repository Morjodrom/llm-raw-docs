"""Shared lock-owning workflow runner. Only document subprocesses use offline_cli."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from workflow_state import checked


class Workflow:
    def __init__(self, root, source, norm_python=None, search_python=None):
        self.root, self.source = checked(root), checked(source)
        runtime = checked(self.root / 'LLM-docs/runtime')
        if self.source == runtime or self.source in runtime.parents or runtime in self.source.parents:
            raise ValueError('input and LLM-docs/runtime must not overlap')
        key = hashlib.sha256(os.fsencode(str(self.root)) + b'\0' + os.fsencode(str(self.source))).hexdigest()
        self.state = checked(runtime / key)
        for name in ('normalized', 'search.sqlite', 'normalize-summary.json', 'index-report.json', 'ready.json', 'ready.tmp', '.lock'):
            checked(self.state / name)
        checked(self.state / 'normalized/documents')
        self.norm_python = norm_python or os.environ.get('DOCNORM_PYTHON', str(self.root / 'docnorm/.venv/bin/python'))
        self.search_python = search_python or os.environ.get('DOCSEARCH_PYTHON', str(self.root / 'docsearch/.venv/bin/python'))
        self.held = False

    @contextmanager
    def lock(self):
        self.state.mkdir(parents=True, exist_ok=True)
        lock = checked(self.state / '.lock')
        try:
            lock.mkdir()
        except FileExistsError:
            raise ValueError(f'collection busy or interrupted lock: {lock} (see README)') from None
        self.held = True
        try:
            yield self
        finally:
            self.held = False
            lock.rmdir()

    def invalidate(self):
        if not self.held:
            raise ValueError('workflow lock required')
        checked(self.state / 'ready.json').unlink(missing_ok=True)

    def update(self):
        if not self.held:
            raise ValueError('workflow lock required')
        self.invalidate()
        offline = self.root / 'LLM-docs/scripts/offline_cli.py'
        steps = [('normalization', self.norm_python, 'normalize-summary.json',
                  ['docnorm', 'normalize', str(self.source), '--output', str(self.state / 'normalized')]),
                 ('indexing', self.search_python, 'index-report.json',
                  ['docsearch', 'index', str(self.state / 'normalized'), '--db', str(self.state / 'search.sqlite'), '--json'])]
        codes = []
        for name, python, report, args in steps:
            if name == 'indexing':
                (self.state / 'normalized/documents').mkdir(parents=True, exist_ok=True)
            with checked(self.state / report).open('w') as out:
                result = subprocess.run([python, str(offline), *args], stdout=out)
            codes.append(result.returncode)
            if result.returncode not in (0, 1):
                raise ValueError(f'{name} failed (exit {result.returncode}); snapshot unavailable')
        result = subprocess.run([self.search_python, str(self.root / 'LLM-docs/scripts/workflow_state.py'),
                                 'ready', str(self.root), str(self.source), *map(str, codes)],
                                capture_output=True, text=True)
        if result.returncode:
            raise ValueError('workflow readiness publication failed')
        return max(codes), json.loads(result.stdout)


def main():
    root, command, source, *args = sys.argv[1:]
    workflow = Workflow(root, source)
    if not workflow.source.is_dir():
        raise ValueError(f'input must be an existing export directory: {workflow.source}')
    with workflow.lock():
        if command == 'update':
            code, report = workflow.update()
            print(json.dumps(report, ensure_ascii=False))
            if code:
                print('local-docs: snapshot available, but partial; inspect status and normalization_report', file=sys.stderr)
            return code
        if not (workflow.state / 'ready.json').is_file() or not (workflow.state / 'search.sqlite').is_file():
            raise ValueError('no completed snapshot; run update --input first')
        if command == 'status':
            report = json.loads((workflow.state / 'ready.json').read_text())
            if report['input'] != str(workflow.source):
                raise ValueError('state belongs to another input')
            print(json.dumps(report, ensure_ascii=False))
            return 0
        return subprocess.call([workflow.search_python, str(workflow.root / 'LLM-docs/scripts/offline_cli.py'),
                                'docsearch', command, '--db', str(workflow.state / 'search.sqlite'), '--json', *args])


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (OSError, ValueError, KeyError) as exc:
        print(f'local-docs: {exc}', file=sys.stderr)
        raise SystemExit(2)
