"""Explicit read-only Drive snapshots. Documents are data, never executable input."""
import argparse
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import signal
import sys
import time
import unicodedata
from urllib.parse import urlparse, parse_qs
import uuid

from workflow_run import Workflow
from workflow_state import checked

SCOPE = 'https://www.googleapis.com/auth/drive.readonly'
API = 'https://www.googleapis.com/drive/v3'
FOLDER = 'application/vnd.google-apps.folder'
SHORTCUT = 'application/vnd.google-apps.shortcut'
EXPORTS = {
    'application/vnd.google-apps.document': ('.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    'application/vnd.google-apps.spreadsheet': ('.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
}
FIELDS = 'id,name,mimeType,webViewLink,version,modifiedTime,md5Checksum,sha256Checksum,size,parents,driveId,capabilities(canDownload),trashed'
OWNER = 'llm-raw-docs-drive-v1'


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def sync_directory(path):
    fd = os.open(checked(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    path = checked(path)
    temp = checked(path.parent / ('.json-' + uuid.uuid4().hex))
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value, out, ensure_ascii=False, sort_keys=True)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path):
    return json.loads(checked(path).read_text(encoding='utf-8'))


def private_dir(path):
    path = checked(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def owned_dir(path, identity):
    path = checked(path)
    marker = checked(path / '.drive-owner.json')
    if path.exists():
        if marker.exists():
            if read_json(marker) != identity:
                raise ValueError('connector directory ownership mismatch')
        elif any(path.iterdir()):
            raise ValueError('refusing to use a nonempty unowned connector directory')
    private_dir(path)
    if not marker.exists():
        atomic_json(marker, identity)
    return path


def external(path, root):
    path, root = checked(path), checked(root)
    if path == root or path in root.parents or root in path.parents:
        raise ValueError('connector storage/configuration must be outside the repository')
    return path


@contextmanager
def directory_lock(path):
    path = checked(path)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        raise ValueError('connector busy or interrupted lock; see recovery instructions') from None
    try:
        yield
    finally:
        path.rmdir()


def folder_reference(value):
    resource_key = None
    if '://' in value:
        parsed = urlparse(value)
        if parsed.scheme != 'https' or parsed.hostname != 'drive.google.com' or parsed.username or parsed.password or parsed.port:
            raise ValueError('expected a standard HTTPS Google Drive folder URL')
        match = re.fullmatch(r'/(?:drive/(?:u/\d+/)?)?folders/([A-Za-z0-9_-]+)/*', parsed.path)
        if not match:
            raise ValueError('expected a Google Drive folder URL')
        value = match.group(1)
        resource_key = parse_qs(parsed.query).get('resourcekey', [None])[0]
        if resource_key and not re.fullmatch(r'[A-Za-z0-9_-]+', resource_key):
            raise ValueError('invalid folder resource key')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value):
        raise ValueError('invalid folder ID')
    return value, resource_key


def component(name, file_id, suffix=None):
    # The ID-derived suffix is stable across equal names, case folding and Unicode normalization.
    text = unicodedata.normalize('NFC', name)
    text = ''.join('_' if ord(c) < 32 or c in '/\\\x7f:' else c for c in text)
    text = text.strip(' .') or 'unnamed'
    original_suffix = Path(text).suffix
    if suffix is None:
        suffix = original_suffix
    if original_suffix:
        text = text[:-len(original_suffix)]
    text = text.encode('utf-8')[:120].decode('utf-8', errors='ignore').strip(' .') or 'unnamed'
    return f'{text}--{digest(file_id)}{suffix}'


def version_stamp(item):
    return tuple(item.get(key) for key in ('version', 'modifiedTime', 'size', 'md5Checksum', 'sha256Checksum', 'trashed', 'name', 'parents'))


class DriveFailure(Exception):
    """Only sanitized errors cross the transport boundary."""


class DriveTransport:
    def __init__(self, session, sleep=time.sleep, jitter=random.random):
        self.session, self.sleep, self.jitter = session, sleep, jitter
        self.resource_keys = {}

    def request(self, endpoint, params=None, target=None):
        import requests
        for attempt in range(5):
            try:
                headers = {}
                if self.resource_keys:
                    headers['X-Goog-Drive-Resource-Keys'] = ','.join(f'{k}/{v}' for k, v in self.resource_keys.items())
                with self.session.get(API + endpoint, params=params, headers=headers,
                                      timeout=(10, 60), stream=target is not None) as response:
                    if response.status_code == 200:
                        if target is None:
                            return response.json()
                        hashes = {key: hashlib.new(key) for key in ('sha256', 'md5')}
                        size = 0
                        with target.open('xb') as out:
                            os.chmod(target, 0o600)
                            for chunk in response.iter_content(1024 * 1024):
                                if chunk:
                                    out.write(chunk)
                                    size += len(chunk)
                                    for h in hashes.values():
                                        h.update(chunk)
                            out.flush()
                            os.fsync(out.fileno())
                        sync_directory(target.parent)
                        return {'size': size, **{k: h.hexdigest() for k, h in hashes.items()}}
                    reasons = []
                    try:
                        reasons = [e.get('reason') for e in response.json().get('error', {}).get('errors', [])]
                    except (ValueError, AttributeError, TypeError):
                        pass
                    transient = response.status_code in (429, 500, 502, 503, 504) or (
                        response.status_code == 403 and any(r in ('rateLimitExceeded', 'userRateLimitExceeded') for r in reasons))
                    if not transient:
                        reason = 'export size limitation' if 'exportSizeLimitExceeded' in reasons else 'access/authentication or request rejected'
                        raise DriveFailure(f'Drive HTTP {response.status_code}: {reason}')
                    try:
                        retry_after = min(60, max(0, float(response.headers.get('Retry-After', '0'))))
                    except ValueError:
                        retry_after = 0
            except (requests.RequestException, ValueError) as exc:
                # URLs, credentials and server error bodies must never appear in diagnostics.
                retry_after = 0
                if isinstance(exc, ValueError) and target is None:
                    raise DriveFailure('invalid Drive response') from None
            finally:
                # Interrupted streams must be retried from byte zero, never appended.
                if target is not None and sys.exc_info()[0] is not None:
                    target.unlink(missing_ok=True)
            if target is not None:
                target.unlink(missing_ok=True)
            if attempt == 4:
                raise DriveFailure('Drive transient failure persisted after five attempts')
            self.sleep(max(retry_after, min(16, 2 ** attempt) + self.jitter()))

    def metadata(self, file_id):
        return self.request('/files/' + file_id, {'fields': FIELDS, 'supportsAllDrives': 'true'})

    def children(self, folder_id, drive_id=None):
        params = {'q': f"'{folder_id}' in parents and trashed = false", 'fields': 'files(' + FIELDS + '),nextPageToken,incompleteSearch',
                  'pageSize': 1000, 'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true',
                  'corpora': 'drive' if drive_id else 'user'}
        if drive_id:
            params['driveId'] = drive_id
        seen = set()
        while True:
            page = self.request('/files', params)
            if page.get('incompleteSearch'):
                raise DriveFailure('incomplete Drive enumeration')
            if not isinstance(page.get('files'), list):
                raise DriveFailure('invalid Drive enumeration response')
            yield from page['files']
            token = page.get('nextPageToken')
            if not token:
                break
            if token in seen:
                raise DriveFailure('repeated Drive pagination token')
            seen.add(token)
            params['pageToken'] = token

    def download(self, item, target, export_mime=None):
        if export_mime:
            return self.request('/files/' + item['id'] + '/export', {'mimeType': export_mime}, target)
        return self.request('/files/' + item['id'], {'alt': 'media', 'supportsAllDrives': 'true'}, target)


class CredentialsStore:
    def __init__(self, root):
        self.root = root
        path = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'llm-raw-docs/drive'
        self.path = owned_dir(external(path, root), {'owner': OWNER, 'kind': 'credentials'})
        self.token = self.path / 'credentials.json'

    def authenticate(self, client_secrets, reauthorize=False):
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import AuthorizedSession
        source = external(client_secrets, self.root)
        config = read_json(source)
        if 'installed' not in config:
            raise ValueError('a Desktop OAuth client JSON is required')
        with directory_lock(self.path / '.lock'):
            if self.token.exists() and not reauthorize:
                raise ValueError('already authorized; use auth --reauthorize to explicitly replace the active account')
            flow = InstalledAppFlow.from_client_config(config, [SCOPE])
            # Suppress library prompts: stdout is reserved for the machine-readable report.
            with redirect_stdout(sys.stderr):
                credentials = flow.run_local_server(host='127.0.0.1', port=0, timeout_seconds=180,
                                                    authorization_prompt_message='Open this URL if the browser does not launch: {url}',
                                                    success_message='Authorization complete. You may close this window.',
                                                    access_type='offline', prompt='consent')
            with AuthorizedSession(credentials) as session:
                account = DriveTransport(session).request('/about', {'fields': 'user(permissionId)'})['user']['permissionId']
            if not credentials.refresh_token:
                raise DriveFailure('OAuth did not issue an offline refresh token; reauthorize with consent')
            atomic_json(self.token, {'account': account, 'credentials': json.loads(credentials.to_json())})
            return {'command': 'auth', 'status': 'authorized', 'account_key': digest(account), 'exit_code': 0}

    @contextmanager
    def session(self):
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import AuthorizedSession, Request
        # Serialize refresh-token writes with auth; credentials never change mid-refresh.
        with directory_lock(self.path / '.lock'):
            saved = read_json(self.token)
            credentials = Credentials.from_authorized_user_info(saved['credentials'], [SCOPE])
            if not credentials.valid:
                credentials.refresh(Request())
            with AuthorizedSession(credentials) as session:
                transport = DriveTransport(session)
                account = transport.request('/about', {'fields': 'user(permissionId)'})['user']['permissionId']
                if account != saved['account']:
                    raise DriveFailure('authenticated account changed; explicit reauthorization required')
                atomic_json(self.token, {'account': account, 'credentials': json.loads(credentials.to_json())})
                yield account, transport
                atomic_json(self.token, {'account': account, 'credentials': json.loads(credentials.to_json())})


class Connector:
    def __init__(self, root, storage, account, folder_id, transport, workflow_factory=Workflow):
        self.root = checked(root)
        self.storage = owned_dir(external(storage, root), {'owner': OWNER, 'kind': 'storage'})
        identity = {'owner': OWNER, 'account_key': digest(account), 'repository': str(self.root), 'folder_id': folder_id}
        key = digest(json.dumps(identity, sort_keys=True))
        self.collection = owned_dir(self.storage / key, identity)
        self.folder_id, self.transport = folder_id, transport
        self.input = checked(self.collection / 'input')
        self.workflow = workflow_factory(self.root, self.input)
        self.reset_report()

    def reset_report(self):
        self.report = {'command': 'refresh', 'folder_id': self.folder_id, 'input': str(self.input), 'sync': 'not_completed',
                       'workflow': 'not_run', 'gaps': [], 'items': [], 'published_snapshot_id': None,
                       'last_successful_sync': None, 'last_usable_indexed_snapshot': None, 'exit_code': 2}

    def published(self):
        path = self.collection / 'published.json'
        return read_json(path) if path.exists() else {}

    def report_published(self):
        published = self.published()
        for key in ('published_snapshot_id', 'last_successful_sync', 'last_usable_indexed_snapshot'):
            self.report[key] = published.get(key)
        self.report['manifest'] = str(self.collection / 'manifests' / (published['published_snapshot_id'] + '.json')) if published else None

    def remove_owned_stage(self, stage):
        stage = checked(stage)
        if stage.parent != self.collection or not re.fullmatch(r'\.stage-[0-9a-f]{32}', stage.name):
            raise ValueError('invalid staging ownership')
        if read_json(stage / '.drive-owner.json') != {'owner': OWNER, 'kind': 'stage'}:
            raise ValueError('unowned staging directory')
        # shutil.rmtree does not follow symlink children; refuse symlink root via checked().
        shutil.rmtree(stage)

    def recover_publication(self):
        journal = checked(self.collection / 'publication.json')
        if not journal.exists():
            return False
        entry = read_json(journal)
        snapshot = entry['published']['published_snapshot_id']
        if not re.fullmatch(r'[0-9a-f]{32}', snapshot):
            raise ValueError('invalid publication journal')
        stage = checked(self.collection / ('.stage-' + snapshot))
        backup = checked(self.collection / ('.previous-' + snapshot))
        if read_json(stage / '.drive-owner.json') != {'owner': OWNER, 'kind': 'stage'}:
            raise ValueError('publication staging ownership mismatch')
        manifest = read_json(self.collection / 'manifests' / (snapshot + '.json'))
        if manifest['snapshot_id'] != snapshot:
            raise ValueError('publication manifest identity mismatch')
        self.workflow.invalidate()
        staged_input = checked(stage / 'input')
        if staged_input.exists():
            if self.input.exists():
                if backup.exists():
                    raise ValueError('ambiguous interrupted publication; manual recovery required')
                self.input.rename(backup)
                sync_directory(self.collection)
            staged_input.rename(self.input)
            sync_directory(self.collection)
            sync_directory(stage)
        if not self.input.is_dir():
            raise ValueError('publication input missing; manual recovery required')
        atomic_json(self.collection / 'published.json', entry['published'])
        # Keep the journal until cleanup completes: an interrupt remains recoverable.
        if backup.exists():
            shutil.rmtree(backup)
        journal.unlink()
        sync_directory(self.collection)
        self.remove_owned_stage(stage)
        return True

    def download_snapshot(self, stage):
        started_at = now()
        # Reuse the scanner's format policy, not a parallel independently drifting list.
        sys.path.insert(0, str(self.root / 'docnorm/src'))
        from docnorm.scanner import ASSETS, TEXT, classify
        files, seen = [], set()
        root = self.transport.metadata(self.folder_id)
        if root.get('mimeType') != FOLDER or root.get('trashed'):
            raise DriveFailure('requested item is not an accessible non-trashed folder')
        stack = [(self.folder_id, Path(), root.get('driveId'))]
        while stack:
            folder_id, relative, drive_id = stack.pop()
            for item in self.transport.children(folder_id, drive_id):
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', item.get('id', '')):
                    raise DriveFailure('invalid Drive file ID')
                if item['id'] in seen:
                    raise DriveFailure('duplicate/cyclic Drive enumeration')
                seen.add(item['id'])
                mime, name = item['mimeType'], item['name']
                record = {k: item.get(k) for k in ('id', 'name', 'mimeType', 'version', 'modifiedTime', 'size', 'md5Checksum', 'sha256Checksum')}
                record['original_url'] = item.get('webViewLink') or 'https://drive.google.com/file/d/' + item['id'] + '/view'
                record['downloaded_at'] = None
                record['parent_id'] = folder_id
                self.report['items'] = files
                if mime == FOLDER:
                    child = relative / component(name, item['id'], '')
                    record.update(relative_path=child.as_posix(), status='folder', export_format=None)
                    files.append(record)
                    stack.append((item['id'], child, item.get('driveId') or drive_id))
                    continue
                export = EXPORTS.get(mime)
                suffix = Path(name).suffix.lower()
                supported = export or (not mime.startswith('application/vnd.google-apps.') and
                                       (suffix in TEXT or suffix in ASSETS or not suffix or name == '.yfm'))
                if name in ('.DS_Store', 'Thumbs.db', 'desktop.ini', 'llms-full.txt') or name.startswith('._'):
                    supported = False
                if not supported:
                    record.update(status='skipped', relative_path=None, export_format=None,
                                  reason='shortcut not followed' if mime == SHORTCUT else 'unsupported format')
                    self.report['gaps'].append({'id': item['id'], 'name': name, 'reason': record['reason']})
                    files.append(record)
                    continue
                path = relative / component(name, item['id'], export[0] if export else ('.yaml' if name == '.yfm' else None))
                record.update(relative_path=path.as_posix(), export_format=export[1] if export else None, status='pending')
                files.append(record)
                target = checked(stage / 'input' / path)
                private_dir(target.parent)
                for attempt in range(3):
                    before = self.transport.metadata(item['id'])
                    if before.get('trashed') or before.get('mimeType') != mime or before.get('capabilities', {}).get('canDownload') is False:
                        raise DriveFailure('required file is inaccessible, trashed, changed type or not downloadable')
                    if before.get('name') != name or (before.get('parents') is not None and folder_id not in before['parents']):
                        raise DriveFailure('required file moved or renamed during enumeration')
                    stats = self.transport.download(before, target, export[1] if export else None)
                    after = self.transport.metadata(item['id'])
                    if version_stamp(before) == version_stamp(after):
                        break
                    target.unlink()
                else:
                    raise DriveFailure('file changed repeatedly during download')
                if not export:
                    if before.get('size') is not None and int(before['size']) != stats['size']:
                        raise DriveFailure('download size mismatch')
                    for remote, local in (('md5Checksum', 'md5'), ('sha256Checksum', 'sha256')):
                        if before.get(remote) and before[remote] != stats[local]:
                            raise DriveFailure('download checksum mismatch')
                record.update({k: after.get(k) for k in ('version', 'modifiedTime', 'size', 'md5Checksum', 'sha256Checksum')})
                record.update(content_sha256=stats['sha256'], downloaded_size=stats['size'], downloaded_at=now(), status='downloaded')
                _, exclusion = classify(target)
                if exclusion:
                    record['reason'] = exclusion
                    self.report['gaps'].append({'id': item['id'], 'name': name, 'reason': exclusion})
        previous = self.published().get('published_snapshot_id')
        if previous:
            old = read_json(self.collection / 'manifests' / (previous + '.json'))
            missing = {item['id'] for item in old['items']} - seen
            for item in old['items']:
                if item['id'] not in missing or item.get('parent_id') in missing:
                    continue
                # A 404 may mean lost access, not deletion. Fail conservatively.
                current = self.transport.metadata(item['id'])
                if not current.get('trashed') and item.get('parent_id') in current.get('parents', []):
                    raise DriveFailure('previous item disappeared from listing without a confirmed removal')
        return {'folder_id': self.folder_id, 'folder_name': root['name'],
                'folder_url': root.get('webViewLink') or 'https://drive.google.com/drive/folders/' + self.folder_id,
                'snapshot_id': stage.name[len('.stage-'):], 'started_at': started_at, 'refreshed_at': now(), 'items': files,
                'limitations': ['Drive enumeration is not transactional; items hidden from this account cannot be detected.',
                                'ID-disambiguated paths can leave original relative document links unresolved.']}

    def refresh(self):
        self.reset_report()
        stage = None
        initial_snapshot = None
        try:
            self.report_published()
            initial_snapshot = self.report['published_snapshot_id']
            with directory_lock(self.collection / '.lock'), self.workflow.lock():
                self.report_published()
                if self.recover_publication():
                    self.report['recovered_publication'] = True
                    self.run_workflow()
                    self.report['recovery_workflow_report'] = self.report.pop('workflow_report')
                    self.report['workflow'] = 'not_run'
                    initial_snapshot = self.report['published_snapshot_id']
                snapshot = uuid.uuid4().hex
                stage = owned_dir(self.collection / ('.stage-' + snapshot), {'owner': OWNER, 'kind': 'stage'})
                private_dir(stage / 'input')
                print('drive-docs: enumerating and downloading accessible documents', file=sys.stderr)
                manifest = self.download_snapshot(stage)
                self.report['items'] = manifest['items']
                private_dir(self.collection / 'manifests')
                atomic_json(self.collection / 'manifests' / (snapshot + '.json'), manifest)
                published = {'published_snapshot_id': snapshot, 'last_successful_sync': manifest['refreshed_at'],
                             'last_usable_indexed_snapshot': self.published().get('last_usable_indexed_snapshot')}
                atomic_json(self.collection / 'publication.json', {'published': published})
                self.recover_publication()
                stage = None
                self.report['sync'] = 'completed_partial' if self.report['gaps'] else 'completed'
                self.report_published()
                print('drive-docs: snapshot published; normalizing and indexing offline', file=sys.stderr)
                self.run_workflow()
                self.report['exit_code'] = int(bool(self.report['gaps'] or self.report['workflow'] == 'completed_partial'))
                self.report['current_index_ready'] = True
        except (Exception, KeyboardInterrupt) as exc:
            # Never include OAuth responses, request URLs, tokens or arbitrary exception strings.
            try:
                self.report_published()
            except (OSError, ValueError, KeyError):
                pass
            changed = self.report['published_snapshot_id'] != initial_snapshot
            if changed and self.report['published_snapshot_id'] is not None:
                self.report['sync'] = 'completed_partial' if self.report['gaps'] else 'completed'
            self.report['error'] = 'Refresh failed; no new snapshot was published.' if self.report['sync'] == 'not_completed' else 'Refresh interrupted or workflow failed after snapshot publication; inspect readiness and recover before querying.'
            if isinstance(exc, DriveFailure):
                self.report['reason'] = str(exc)
            self.report['publication_pending'] = (self.collection / 'publication.json').exists()
            self.report['current_index_ready'] = (self.workflow.state / 'ready.json').is_file()
            for item in self.report['items']:
                if item['status'] == 'pending':
                    item.update(status='failed', reason='required download did not complete')
            self.report['exit_code'] = 2
        finally:
            if stage is not None and not (self.collection / 'publication.json').exists():
                self.remove_owned_stage(stage)
        return self.report['exit_code'], self.report

    def run_workflow(self):
        self.report['workflow'] = 'failed'
        code, result = self.workflow.update()
        self.report['workflow'] = 'completed_partial' if code else 'completed'
        self.report['workflow_report'] = result
        published = self.published()
        published['last_usable_indexed_snapshot'] = published['published_snapshot_id']
        atomic_json(self.collection / 'published.json', published)
        self.report_published()


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('invalid arguments; use --help')


def main(argv=None):
    root = Path(__file__).resolve().parents[2]
    parser = Parser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    auth = commands.add_parser('auth')
    auth.add_argument('--client-secrets', required=True, type=Path)
    auth.add_argument('--reauthorize', action='store_true')
    refresh = commands.add_parser('refresh')
    refresh.add_argument('--folder', required=True)
    refresh.add_argument('--storage-root', type=Path, default=Path.home() / '.local/share/llm-raw-docs/drive')
    report = None
    try:
        args = parser.parse_args(argv)
        if args.command == 'auth':
            store = CredentialsStore(root)
            report = store.authenticate(args.client_secrets, args.reauthorize)
            code = 0
        else:
            folder, resource_key = folder_reference(args.folder)
            store = CredentialsStore(root)
            # Read local identity first so auth failure can still identify the
            # prior snapshot. No cached token is sent anywhere except OAuth.
            account = read_json(store.token)['account']
            cached_account = account
            connector = Connector(root, args.storage_root, account, folder, None)
            connector.report_published()
            report = connector.report
            with store.session() as (account, transport):
                if account != cached_account:
                    raise DriveFailure('active account changed during refresh startup; retry explicitly')
                if resource_key:
                    transport.resource_keys[folder] = resource_key
                connector.transport = transport
                code, report = connector.refresh()
        print(json.dumps(report, ensure_ascii=False))
        return code
    except (Exception, KeyboardInterrupt) as exc:
        if report is None:
            report = {'command': 'drive-docs', 'sync': 'not_completed', 'workflow': 'not_run',
                      'input': None, 'published_snapshot_id': None, 'last_successful_sync': None,
                      'last_usable_indexed_snapshot': None}
        report.update(exit_code=2, status='failed', error='Authentication, configuration, or refresh failed. Check setup, permissions and locks; no credentials are included in diagnostics.')
        if isinstance(exc, DriveFailure):
            report['reason'] = str(exc)
        print(json.dumps(report, ensure_ascii=False))
        print('drive-docs: failed; see JSON report and documented recovery instructions', file=sys.stderr)
        return 2


if __name__ == '__main__':
    def interrupted(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
