"""Owner-authorized browser job completion, source ingestion, and capture bundles."""
import io
import hashlib
import tempfile
import json
import os
from pathlib import Path
import stat
import zipfile


def _write_private(path, data):
    if path.is_symlink(): raise ValueError('Capture record cannot be a symlink')
    fd, temporary = tempfile.mkstemp(prefix='.capture-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def persist_capture(server, capture, job_id):
    with server.lock:
        completed = getattr(server, '_capture_results', {})
        if job_id in completed: return
        imported = server.memory.import_chats(capture['parsed'])
        reports = server.state_dir / 'browser-captures'
        if reports.is_symlink(): raise ValueError('Capture reports folder cannot be a symlink')
        reports.mkdir(mode=0o700, exist_ok=True)
        record = reports / (hashlib.sha256(job_id.encode()).hexdigest() + '.json')
        _write_private(record, capture)
        _write_private(server.state_dir / 'browser-capture.json', capture)
        completed[job_id] = imported
        server._capture_results = completed
        server._capture_job = job_id
        server._capture_imported = imported
        server.journal.write('memory.browser.capture', 'Imported rendered browser chats as unconfirmed source history', imported)


def status(server):
    result = server.browser_import.status()
    job = result.get('job') or {}
    capture = result.get('result') or {}
    if job.get('action') == 'capture' and job.get('state') == 'complete' and capture.get('parsed'):
        # Production persists in the worker completion callback, before another
        # action can replace its result. This idempotent fallback serves older jobs.
        persist_capture(server, capture, job['id'])
        result['result'] = {k: v for k, v in capture.items() if k not in ('parsed', 'files')}
        result['result'].update(imported=server._capture_results[job['id']], bundle_available=True)
    elif (server.state_dir / 'browser-capture.json').is_file():
        result['bundle_available'] = True
    return result


def action(server, name, data):
    if data.get('confirm') is not True:
        raise ValueError('Confirm this browser action first')
    manager = server.browser_import
    if name == 'open': return manager.open(data.get('provider'))
    if name == 'export': return manager.request_export()
    if name == 'scan': return manager.scan()
    if name == 'capture': return manager.capture(data.get('urls'), data.get('capture_files', False))
    if name == 'close': return manager.close_browser()
    raise ValueError('Unknown browser action')


def bundle(server):
    """ZIP normalized sources, a reconstruction ledger and downloaded inert files."""
    path = server.state_dir / 'browser-capture.json'
    if not path.is_file() or path.is_symlink(): raise ValueError('No collected browser capture is available')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream: capture = json.load(stream)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('conversations.json', json.dumps(dict(capture['parsed'], format='mainaiger.browser-capture.v1'), ensure_ascii=False, indent=2))
        manifest = {k: v for k, v in capture.items() if k not in ('parsed', 'files')}
        manifest['files'] = []
        total = 0
        allowed = (server.state_dir / 'browser-import-files').resolve()
        for item in capture.get('files', []):
            file = Path(item['path'])
            if file.is_symlink() or not file.is_file() or allowed not in file.resolve().parents:
                raise ValueError('A captured attachment is missing or outside the private capture folder')
            info = file.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 25 * 1024 * 1024:
                raise ValueError('Captured attachment exceeds its limit')
            total += info.st_size
            if total > 100 * 1024 * 1024: raise ValueError('Capture exceeds the bundle size limit')
            # IDs are generated locally, never provider-controlled paths.
            name = 'files/' + file.resolve().relative_to(allowed).as_posix()
            archive.write(file, name)
            manifest['files'].append({k: v for k, v in item.items() if k != 'path'} | {'bundle_path': name})
        archive.writestr('capture-report.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr('README.txt', 'MainAIger browser reconstruction. Only accessible rendered text and successfully downloaded attachments are included.\nInspect capture-report.json for gaps. This is not a full original provider export or confirmed memory.\n')
    return output.getvalue()
