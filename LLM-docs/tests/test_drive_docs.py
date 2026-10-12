"""Drive refresh tests: no credentials, Google calls, or embedded document execution."""
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'LLM-docs/scripts'))
import drive_docs as drive
from workflow_run import Workflow


def file_item(id='text', name='сеть.md', mime='text/markdown', parents=None):
    return {'id': id, 'name': name, 'mimeType': mime, 'version': '1', 'modifiedTime': '2026-10-12',
            'parents': ['root'] if parents is None else parents, 'capabilities': {'canDownload': True}}


class FakeDrive:
    def __init__(self, items=None):
        self.items = items if items is not None else [file_item()]
        self.contents = {'text': '# Сеть\n\n## Ошибки\nОшибка подключения сервера.\n'.encode()}
        self.extra = {}
        self.failure = None
        self.downloads = []
        self.resource_keys = {}

    def metadata(self, id):
        if self.failure == 'auth':
            raise RuntimeError('secret-token-from-OAuth')
        if id == 'root':
            return file_item('root', 'Folder', drive.FOLDER)
        if id in self.extra:
            return dict(self.extra[id])
        for item in self.items:
            if item['id'] == id:
                return dict(item)
        raise drive.DriveFailure('required item is inaccessible')

    def children(self, id, drive_id=None):
        if self.failure == 'list':
            raise drive.DriveFailure('listing failed')
        return [dict(i) for i in self.items if id in i['parents'] and not i.get('trashed')]

    def download(self, item, target, export_mime=None):
        target.write_bytes(self.contents.get(item['id'], b'# Guide\nBody\n'))
        self.downloads.append((item['id'], export_mime))
        if self.failure == 'download':
            raise drive.DriveFailure('required file download failed')
        if self.failure == 'interrupt':
            raise KeyboardInterrupt
        data = target.read_bytes()
        return {'size': len(data), 'md5': hashlib.md5(data).hexdigest(), 'sha256': hashlib.sha256(data).hexdigest()}


class FakeWorkflow(Workflow):
    code = 0

    def update(self):
        self.invalidate()
        if self.code == 2:
            raise ValueError('fatal processing failure')
        drive.atomic_json(self.state / 'ready.json', {'input': str(self.source)})
        return self.code, {'input': str(self.source), 'state': str(self.state)}


@pytest.fixture
def collection():
    with tempfile.TemporaryDirectory(prefix='drive тест ', dir='/private/tmp') as folder:
        transport = FakeDrive()
        connector = drive.Connector(ROOT, Path(folder) / 'storage', 'account', 'root', transport, FakeWorkflow)
        yield connector, transport
        shutil.rmtree(connector.workflow.state, ignore_errors=True)


def refresh(connector, code=0):
    result_code, report = connector.refresh()
    assert result_code == code, report
    return report


def test_refresh_hashes_and_manifest_are_outside_input(collection):
    connector, transport = collection
    report = refresh(connector)
    assert report['sync'] == report['workflow'] == 'completed'
    assert report['published_snapshot_id'] == report['last_usable_indexed_snapshot']
    record = report['items'][0]
    assert (connector.input / record['relative_path']).read_bytes() == transport.contents['text']
    assert record['content_sha256'] == hashlib.sha256(transport.contents['text']).hexdigest()
    manifest = drive.read_json(connector.collection / 'manifests' / (report['published_snapshot_id'] + '.json'))
    assert manifest['folder_id'] == 'root'
    assert list(connector.input.iterdir())[0].suffix == '.md'
    assert not (connector.input / 'published.json').exists()


@pytest.mark.parametrize('failure', ['auth', 'list', 'download', 'interrupt'])
def test_failed_sync_preserves_previous_source_and_readiness(collection, failure):
    connector, transport = collection
    first = refresh(connector)
    files = {p.name: p.read_bytes() for p in connector.input.iterdir()}
    readiness = (connector.workflow.state / 'ready.json').read_bytes()
    transport.failure = failure

    report = refresh(connector, 2)

    assert report['sync'] == 'not_completed'
    assert report['workflow'] == 'not_run'
    assert report['published_snapshot_id'] == first['published_snapshot_id']
    assert {p.name: p.read_bytes() for p in connector.input.iterdir()} == files
    assert (connector.workflow.state / 'ready.json').read_bytes() == readiness
    assert 'secret-token' not in json.dumps(report)
    assert not list(connector.collection.glob('.stage-*'))


def test_unchanged_changed_and_confirmed_deletion(collection):
    connector, transport = collection
    first = refresh(connector)
    second = refresh(connector)
    assert first['items'][0]['content_sha256'] == second['items'][0]['content_sha256']
    assert len(transport.downloads) == 2  # full download, no cache
    transport.contents['text'] = b'# New\nChanged\n'
    third = refresh(connector)
    assert third['items'][0]['content_sha256'] != second['items'][0]['content_sha256']
    transport.extra['text'] = dict(transport.items[0], trashed=True)
    transport.items = []
    refresh(connector)
    assert not list(connector.input.iterdir())


def test_missing_item_without_confirmed_deletion_fails(collection):
    connector, transport = collection
    refresh(connector)
    transport.items = []
    report = refresh(connector, 2)
    assert report['current_index_ready']
    assert list(connector.input.iterdir())


def test_recursive_native_exports_duplicate_names_and_shortcuts(collection):
    connector, transport = collection
    transport.items = [file_item('folder', 'раздел / ..', drive.FOLDER),
                       file_item('doc', 'Guide', 'application/vnd.google-apps.document', ['folder']),
                       file_item('sheet', 'Guide', 'application/vnd.google-apps.spreadsheet', ['folder']),
                       file_item('one', 'same.md'), file_item('two', 'same.md'),
                       file_item('shortcut', 'link', drive.SHORTCUT),
                       file_item('pdf', 'unsupported.pdf', 'application/pdf')]
    report = refresh(connector, 1)
    assert len(report['gaps']) == 2
    assert ('doc', drive.EXPORTS['application/vnd.google-apps.document'][1]) in transport.downloads
    assert ('sheet', drive.EXPORTS['application/vnd.google-apps.spreadsheet'][1]) in transport.downloads
    paths = [i['relative_path'] for i in report['items'] if i['status'] == 'downloaded']
    assert len(paths) == len(set(paths)) == 4
    assert all('..' not in Path(p).parts for p in paths)


@pytest.mark.parametrize('name', ['../x.md', '/x.md', 'x\\y.md', '\x00.md', ' . ', 'Руководство 😀.md', 'a' * 500 + '.md'])
def test_path_components_are_bounded_and_inert(name):
    result = drive.component(name, 'id')
    assert '/' not in result and '\\' not in result and '\x00' not in result
    assert len(result.encode()) < 255
    assert result not in ('.', '..')
    assert result != drive.component(name, 'another-id')


@pytest.mark.parametrize('code', [1, 2])
def test_partial_and_fatal_workflow_after_publication(collection, code):
    connector, transport = collection
    first = refresh(connector)
    connector.workflow.code = code
    transport.contents['text'] = b'# New\nNew content\n'
    report = refresh(connector, code)
    assert report['sync'] == 'completed'
    assert report['published_snapshot_id'] != first['published_snapshot_id']
    if code == 1:
        assert report['workflow'] == 'completed_partial'
        assert report['last_usable_indexed_snapshot'] == report['published_snapshot_id']
    else:
        assert report['workflow'] == 'failed'
        assert not (connector.workflow.state / 'ready.json').exists()
        assert report['last_usable_indexed_snapshot'] == first['published_snapshot_id']


@pytest.mark.parametrize('which', ['connector', 'workflow'])
def test_lock_contention_never_downloads(collection, which):
    connector, transport = collection
    path = connector.collection / '.lock' if which == 'connector' else connector.workflow.state / '.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()
    try:
        refresh(connector, 2)
        assert not transport.downloads
        assert path.is_dir()
    finally:
        path.rmdir()


def test_changed_during_download_has_bounded_restarts(collection):
    connector, transport = collection
    original = transport.metadata
    counter = 0
    def changing(id):
        nonlocal counter
        result = original(id)
        if id != 'root':
            counter += 1
            result['version'] = str(counter)
        return result
    transport.metadata = changing
    refresh(connector, 2)
    assert len(transport.downloads) == 3
    assert not connector.input.exists()


def test_account_namespacing_and_unowned_storage(collection):
    connector, transport = collection
    other = drive.Connector(ROOT, connector.storage, 'different-account', 'root', transport, FakeWorkflow)
    assert connector.input != other.input
    shutil.rmtree(other.workflow.state, ignore_errors=True)
    with pytest.raises(ValueError):
        drive.Connector(ROOT, ROOT, 'account', 'root', transport)
    path = connector.storage.parent / 'unowned'
    path.mkdir(); (path / 'keep').write_text('valuable')
    with pytest.raises(ValueError):
        drive.Connector(ROOT, path, 'account', 'root', transport)
    assert (path / 'keep').read_text() == 'valuable'
    link = connector.storage.parent / 'link'
    link.symlink_to(connector.storage)
    with pytest.raises(ValueError):
        drive.Connector(ROOT, link, 'account', 'root', transport)


@pytest.mark.parametrize('phase', ['prepared', 'old_moved', 'new_moved'])
def test_interrupted_publication_recovers_under_both_locks(collection, phase):
    connector, _ = collection
    refresh(connector)
    snapshot = 'a' * 32
    stage = drive.owned_dir(connector.collection / ('.stage-' + snapshot), {'owner': drive.OWNER, 'kind': 'stage'})
    drive.private_dir(stage / 'input')
    (stage / 'input/new.md').write_text('# New\nRecovered\n')
    published = {'published_snapshot_id': snapshot, 'last_successful_sync': drive.now(), 'last_usable_indexed_snapshot': None}
    drive.atomic_json(connector.collection / 'manifests' / (snapshot + '.json'), {'snapshot_id': snapshot, 'items': []})
    drive.atomic_json(connector.collection / 'publication.json', {'published': published})
    if phase != 'prepared':
        connector.input.rename(connector.collection / ('.previous-' + snapshot))
    if phase == 'new_moved':
        (stage / 'input').rename(connector.input)
    with drive.directory_lock(connector.collection / '.lock'), connector.workflow.lock():
        assert connector.recover_publication()
        assert not (connector.workflow.state / 'ready.json').exists()
    assert (connector.input / 'new.md').read_text().startswith('# New')
    assert not (connector.collection / 'publication.json').exists()


class Response:
    def __init__(self, status=200, data=None, chunks=None, headers=None):
        self.status_code, self.data, self.chunks, self.headers = status, data, chunks, headers or {}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def json(self):
        return self.data
    def iter_content(self, size):
        yield from self.chunks


def test_drive_pagination_and_shared_drive_parameters():
    session = Mock()
    session.get.side_effect = [Response(data={'files': [file_item()], 'nextPageToken': 'next'}), Response(data={'files': []})]
    transport = drive.DriveTransport(session)
    assert len(list(transport.children('root', 'shared-drive'))) == 1
    params = session.get.call_args.kwargs['params']
    assert params['corpora'] == 'drive' and params['driveId'] == 'shared-drive'
    assert params['supportsAllDrives'] == params['includeItemsFromAllDrives'] == 'true'
    assert params['pageToken'] == 'next'
    session.get.side_effect = [Response(data={'incompleteSearch': True})]
    with pytest.raises(drive.DriveFailure):
        list(transport.children('root'))


def test_retries_rate_limits_and_persistent_errors():
    session, sleeps = Mock(), []
    session.get.side_effect = [Response(403, {'error': {'errors': [{'reason': 'rateLimitExceeded'}]}}, headers={'Retry-After': '999'}),
                               Response(data={'id': 'root'})]
    transport = drive.DriveTransport(session, sleeps.append, lambda: 0)
    assert transport.metadata('root')['id'] == 'root'
    assert sleeps == [60]
    session.get.side_effect = [Response(503)] * 5
    sleeps.clear()
    with pytest.raises(drive.DriveFailure, match='five attempts'):
        transport.metadata('root')
    assert sleeps == [1, 2, 4, 8]
    session.get.side_effect = [Response(401, {'error': {'message': 'secret-token'}})]
    with pytest.raises(drive.DriveFailure) as error:
        transport.metadata('root')
    assert 'secret-token' not in str(error.value)


def test_interrupted_stream_restarts_from_zero(collection):
    import requests
    connector, _ = collection
    def chunks():
        yield b'partial'
        raise requests.ConnectionError('secret-header')
    session = Mock()
    session.get.side_effect = [Response(chunks=chunks()), Response(chunks=[b'complete'])]
    target = connector.collection / 'download'
    result = drive.DriveTransport(session, lambda _: None).request('/files/id', target=target)
    assert target.read_bytes() == b'complete'
    assert result['sha256'] == hashlib.sha256(b'complete').hexdigest()


@pytest.mark.parametrize('value', ['root', 'https://drive.google.com/drive/folders/root', 'https://drive.google.com/drive/u/0/folders/root?usp=sharing'])
def test_folder_url_and_id(value):
    assert drive.folder_reference(value)[0] == 'root'


@pytest.mark.parametrize('value', ['../root', 'https://evil.com/drive/folders/root', 'https://drive.google.com/file/d/root/view'])
def test_invalid_folder_reference(value):
    with pytest.raises(ValueError):
        drive.folder_reference(value)


def test_real_refresh_russian_search_and_full_section(collection):
    connector, transport = collection
    connector.workflow = Workflow(ROOT, connector.input, sys.executable, sys.executable)
    report = refresh(connector)
    env = dict(os.environ, DOCNORM_PYTHON=sys.executable, DOCSEARCH_PYTHON=sys.executable)
    def invoke(command, *args):
        return subprocess.run(['bash', str(ROOT / 'LLM-docs/local-docs.sh'), command, '--input', report['input'], *args],
                              capture_output=True, text=True, env=env)
    search = invoke('search', 'ошибки подключения')
    assert search.returncode == 0, search.stderr
    hit = json.loads(search.stdout)['results'][0]
    section = invoke('read', hit['section_id'])
    assert section.returncode == 0
    assert 'Ошибка подключения сервера.' in json.loads(section.stdout)['original_markdown']
    # Failed synchronization preserves the actual index, not just a mock readiness marker.
    old_db = (connector.workflow.state / 'search.sqlite').read_bytes()
    transport.failure = 'list'
    refresh(connector, 2)
    assert (connector.workflow.state / 'search.sqlite').read_bytes() == old_db
    assert invoke('search', 'ошибки подключения').returncode == 0


@pytest.fixture
def oauth_store(collection, monkeypatch):
    connector, _ = collection
    monkeypatch.setenv('XDG_CONFIG_HOME', str(connector.storage.parent / 'config'))
    store = drive.CredentialsStore(ROOT)
    secrets = connector.storage.parent / 'client.json'
    secrets.write_text(json.dumps({'installed': {'client_id': 'team-client', 'client_secret': 'client-private-value',
                                              'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                                              'token_uri': 'https://oauth2.googleapis.com/token'}}))
    credentials = Mock()
    credentials.refresh_token = 'refresh-private-value'
    credentials.valid = True
    credentials.to_json.return_value = json.dumps({'token': 'access-private-value', 'refresh_token': 'refresh-private-value'})
    flow = Mock(credentials=credentials)
    flow.run_local_server.return_value = credentials
    import google_auth_oauthlib.flow
    import google.auth.transport.requests
    import google.oauth2.credentials
    monkeypatch.setattr(google_auth_oauthlib.flow.InstalledAppFlow, 'from_client_config', Mock(return_value=flow))
    session = Mock()
    session.get.return_value = Response(data={'user': {'permissionId': 'account'}})
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(google.auth.transport.requests, 'AuthorizedSession', Mock(return_value=session))
    monkeypatch.setattr(google.oauth2.credentials.Credentials, 'from_authorized_user_info', Mock(return_value=credentials))
    return store, secrets, credentials, flow, session


def test_oauth_loopback_scope_permissions_and_explicit_reauthorization(oauth_store, capsys):
    store, secrets, credentials, flow, _ = oauth_store
    report = store.authenticate(secrets)
    assert report['status'] == 'authorized'
    assert store.path.stat().st_mode & 0o777 == 0o700
    assert store.token.stat().st_mode & 0o777 == 0o600
    arguments = flow.run_local_server.call_args.kwargs
    assert arguments['host'] == '127.0.0.1' and arguments['port'] == 0
    assert arguments['timeout_seconds'] == 180 and arguments['access_type'] == 'offline'
    from google_auth_oauthlib.flow import InstalledAppFlow
    assert InstalledAppFlow.from_client_config.call_args.args[1] == [drive.SCOPE]
    with pytest.raises(ValueError, match='reauthorize'):
        store.authenticate(secrets)
    store.authenticate(secrets, reauthorize=True)
    captured = capsys.readouterr()
    assert 'private-value' not in captured.out + captured.err + json.dumps(report)
    assert not (store.path / '.lock').exists()


def test_oauth_refresh_and_account_mismatch_do_not_overwrite_credentials(oauth_store):
    store, secrets, credentials, _, session = oauth_store
    store.authenticate(secrets)
    credentials.valid = False
    with store.session() as (account, _):
        assert account == 'account'
    credentials.refresh.assert_called_once()
    previous = store.token.read_bytes()
    session.get.return_value = Response(data={'user': {'permissionId': 'another-account'}})
    with pytest.raises(drive.DriveFailure, match='account changed'):
        with store.session():
            pass
    assert store.token.read_bytes() == previous


def test_authentication_failure_reports_no_credentials(oauth_store, monkeypatch, capsys):
    store, secrets, _, flow, _ = oauth_store
    flow.run_local_server.side_effect = RuntimeError('access-private-value')
    monkeypatch.setattr(drive, 'CredentialsStore', lambda _: store)
    assert drive.main(['auth', '--client-secrets', str(secrets)]) == 2
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report['workflow'] == 'not_run'
    assert 'private-value' not in captured.out + captured.err
    assert not store.token.exists()


def test_usage_failure_is_one_json_result(capsys):
    assert drive.main(['refresh']) == 2
    assert json.loads(capsys.readouterr().out)['exit_code'] == 2


def test_unsupported_extensionless_binary_reports_partial(collection):
    connector, transport = collection
    transport.items = [file_item('binary', 'extensionless', 'application/octet-stream')]
    transport.contents['binary'] = b'\x00\x01\x02'
    report = refresh(connector, 1)
    assert report['gaps'][0]['reason'] == 'unsupported format'


def test_download_size_and_checksum_mismatch_abort(collection):
    connector, transport = collection
    transport.items[0]['md5Checksum'] = 'bad-checksum'
    refresh(connector, 2)
    assert not connector.input.exists()


def test_known_inaccessible_file_is_itemized(collection):
    connector, transport = collection
    transport.items[0]['capabilities']['canDownload'] = False
    report = refresh(connector, 2)
    assert report['items'][0]['status'] == 'failed'
    assert not transport.downloads


def test_workflow_update_requires_held_lock(collection):
    connector, _ = collection
    with pytest.raises(ValueError, match='lock required'):
        connector.workflow.update()


def test_both_locks_cover_download_and_block_other_refresh_and_local_update(collection):
    connector, transport = collection
    refresh(connector)
    original = transport.download
    def during_download(*args):
        assert (connector.collection / '.lock').is_dir()
        assert (connector.workflow.state / '.lock').is_dir()
        competing = drive.Connector(ROOT, connector.storage, 'account', 'root', transport, FakeWorkflow)
        assert competing.refresh()[0] == 2
        env = dict(os.environ, DOCNORM_PYTHON=sys.executable, DOCSEARCH_PYTHON=sys.executable)
        result = subprocess.run(['bash', str(ROOT / 'LLM-docs/local-docs.sh'), 'update', '--input', str(connector.input)],
                                env=env, capture_output=True, text=True)
        assert result.returncode == 2 and 'busy' in result.stderr
        return original(*args)
    transport.download = during_download
    refresh(connector)
    assert not (connector.collection / '.lock').exists()
    assert not (connector.workflow.state / '.lock').exists()


def test_native_exports_are_streamed_with_correct_mime_types(collection):
    connector, _ = collection
    session = Mock()
    transport = drive.DriveTransport(session)
    for mime, (_, exported) in drive.EXPORTS.items():
        target = connector.collection / ('export' + drive.EXPORTS[mime][0])
        session.get.return_value = Response(chunks=[b'export-bytes'])
        transport.download(file_item('native', 'Native', mime), target, exported)
        assert session.get.call_args.args[0].endswith('/files/native/export')
        assert session.get.call_args.kwargs['params'] == {'mimeType': exported}
        assert target.read_bytes() == b'export-bytes'


def test_auth_failure_on_refresh_identifies_local_prior_snapshot(oauth_store, collection, monkeypatch, capsys):
    store, secrets, credentials, _, _ = oauth_store
    connector, _ = collection
    first = refresh(connector)
    store.authenticate(secrets)
    credentials.valid = False
    credentials.refresh.side_effect = RuntimeError('refresh-private-value')
    monkeypatch.setattr(drive, 'CredentialsStore', lambda _: store)
    assert drive.main(['refresh', '--folder', 'root', '--storage-root', str(connector.storage)]) == 2
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report['published_snapshot_id'] == first['published_snapshot_id']
    assert report['last_usable_indexed_snapshot'] == first['published_snapshot_id']
    assert report['workflow'] == 'not_run'
    assert 'private-value' not in output.out + output.err


def test_real_processing_partial_and_fatal_readiness(collection):
    connector, transport = collection
    transport.items.append(file_item('bad', 'bad.json', 'application/json'))
    transport.contents['bad'] = b'{invalid'
    connector.workflow = Workflow(ROOT, connector.input, sys.executable, sys.executable)
    report = refresh(connector, 1)
    assert report['workflow_report']['normalization_exit'] == 1
    previous = report['published_snapshot_id']
    failing = connector.storage.parent / 'failing-python'
    failing.write_text('#!/bin/sh\nexit 2\n'); failing.chmod(0o700)
    connector.workflow.norm_python = str(failing)
    report = refresh(connector, 2)
    assert report['sync'] == 'completed' and report['workflow'] == 'failed'
    assert report['published_snapshot_id'] != previous
    assert not (connector.workflow.state / 'ready.json').exists()


def test_transport_http_export_limit_is_fatal_without_retries():
    session = Mock()
    session.get.return_value = Response(403, {'error': {'errors': [{'reason': 'exportSizeLimitExceeded'}]}})
    with pytest.raises(drive.DriveFailure, match='export size limitation'):
        drive.DriveTransport(session).metadata('root')
    assert session.get.call_count == 1


@pytest.mark.parametrize('tool,command', [('docnorm', 'normalize'), ('docsearch', 'index'), ('docsearch', 'search'), ('docsearch', 'read')])
def test_every_document_stage_enters_with_network_disabled(tool, command):
    offline = ROOT / 'LLM-docs/scripts/offline_cli.py'
    code = f'''
import runpy, socket, sys
from unittest.mock import patch
def probe(*args, **kwargs):
    for action in (lambda: socket.create_connection(("example.com", 443)),
                   lambda: socket.getaddrinfo("example.com", 443),
                   lambda: socket.socket().connect(("127.0.0.1", 80))):
        try:
            action()
        except RuntimeError as exc:
            assert "network access is forbidden" in str(exc)
        else:
            raise AssertionError("document runtime contacted network")
sys.argv = [{str(offline)!r}, {tool!r}, {command!r}]
with patch.object(runpy, 'run_module', probe), patch.object(runpy, 'run_path', probe):
    exec(compile(open({str(offline)!r}).read(), {str(offline)!r}, 'exec'), {{'__file__': {str(offline)!r}}})
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_malformed_listing_never_becomes_empty_snapshot():
    session = Mock()
    session.get.return_value = Response(data={'unexpected': 'not-a-listing'})
    with pytest.raises(drive.DriveFailure, match='invalid Drive enumeration'):
        list(drive.DriveTransport(session).children('root'))


def test_credentials_and_configuration_cannot_be_written_in_repository(oauth_store, monkeypatch):
    store, secrets, _, _, _ = oauth_store
    with pytest.raises(ValueError, match='outside the repository'):
        store.authenticate(ROOT / 'client-secrets.json')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(ROOT))
    with pytest.raises(ValueError, match='outside the repository'):
        drive.CredentialsStore(ROOT)
    assert not (ROOT / 'client-secrets.json').exists()


def test_extensionless_yaml_settings_keep_yaml_format(collection):
    connector, transport = collection
    transport.items = [file_item('settings', '.yfm', 'text/plain')]
    transport.contents['settings'] = b'title: Test\n'
    report = refresh(connector)
    assert report['items'][0]['relative_path'].endswith('.yaml')


@pytest.mark.parametrize('index_code', [1, 2])
def test_indexing_partial_and_fatal_results_are_distinct_from_sync(collection, monkeypatch, index_code):
    connector, _ = collection
    connector.workflow = Workflow(ROOT, connector.input, sys.executable, sys.executable)
    first = refresh(connector)
    prior_db = (connector.workflow.state / 'search.sqlite').read_bytes()
    original = subprocess.run
    def index_outcome(args, **kwargs):
        if args[2:4] == ['docsearch', 'index']:
            json.dump({'failed': 1, 'retained_stale': 1, 'stale_documents': ['example']}, kwargs['stdout'])
            return subprocess.CompletedProcess(args, index_code)
        return original(args, **kwargs)
    monkeypatch.setattr(subprocess, 'run', index_outcome)
    report = refresh(connector, index_code)
    assert report['sync'] == 'completed'
    assert (connector.workflow.state / 'search.sqlite').read_bytes() == prior_db
    if index_code == 1:
        assert report['workflow'] == 'completed_partial'
        assert report['workflow_report']['normalization_exit'] == 0
        assert report['workflow_report']['index_exit'] == 1
        assert report['workflow_report']['index']['retained_stale'] == 1
    else:
        assert report['workflow'] == 'failed'
        assert report['last_usable_indexed_snapshot'] == first['published_snapshot_id']
        assert not (connector.workflow.state / 'ready.json').exists()


def test_reauthorization_race_cannot_write_other_accounts_collection(oauth_store, collection, monkeypatch, capsys):
    store, secrets, _, _, _ = oauth_store
    connector, transport = collection
    first = refresh(connector)
    store.authenticate(secrets)
    downloads = len(transport.downloads)
    @contextmanager
    def changed_account_session():
        yield 'different-account', transport
    monkeypatch.setattr(store, 'session', changed_account_session)
    monkeypatch.setattr(drive, 'CredentialsStore', lambda _: store)
    assert drive.main(['refresh', '--folder', 'root', '--storage-root', str(connector.storage)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report['published_snapshot_id'] == first['published_snapshot_id']
    assert len(transport.downloads) == downloads
