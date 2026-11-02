"""Opt-in GitHub release updater for DDO Combat Analyzer (Windows and Linux X11).

Only official published Releases from JanBSorensen/DDO-Combat-Analyzer are used.
The running GUI does no file replacements: it downloads and checks the platform
archive, then starts a separate helper that waits for the GUI to exit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from pathlib import Path, PurePosixPath

API = 'https://api.github.com/repos/JanBSorensen/DDO-Combat-Analyzer/releases/latest'
RELEASE_PAGE = 'https://github.com/JanBSorensen/DDO-Combat-Analyzer/releases/latest'
REPO = 'JanBSorensen/DDO-Combat-Analyzer'
ASSETS = {
    'windows': 'DDO-Combat-Analyzer-Windows.zip',
    'linux': 'DDO-Combat-Analyzer-Linux.zip',
}
VERSION_RE = re.compile(r'^v\d{4}\.\d{1,2}\.\d{1,2}-[0-9a-f]{8}$')
MAX_ENTRIES = 150
MAX_ENTRY_SIZE = 12 * 1024 * 1024
MAX_TOTAL_SIZE = 45 * 1024 * 1024
HEADERS = {'User-Agent': 'DDO-Combat-Analyzer-Updater/1.0', 'Accept': 'application/vnd.github+json'}


def platform_key():
    if sys.platform.startswith('win'):
        return 'windows'
    if sys.platform.startswith('linux'):
        return 'linux'
    raise RuntimeError(f'Automatic updates are not supported on {sys.platform}.')


def local_revision(install_root):
    version_file = Path(install_root) / 'VERSION.txt'
    try:
        match = re.search(r'^Source commit:\s*([0-9a-f]{40})\s*$',
                          version_file.read_text(encoding='utf-8'), re.M)
        return match.group(1) if match else None
    except OSError:
        return None


def is_test_install(install_root):
    root = Path(install_root)
    return (root / 'LOCAL_TEST_VERSION.txt').exists() or local_revision(root) is None


def _https(url):
    result = urllib.parse.urlparse(url)
    if result.scheme != 'https' or not result.netloc:
        raise RuntimeError('Refusing a non-HTTPS release URL.')
    return url


def fetch_latest_release(platform=None, opener=None):
    """Read official GitHub metadata; do not trust arbitrary asset names or URLs."""
    platform = platform or platform_key()
    if platform not in ASSETS:
        raise ValueError('Unsupported update platform')
    request = urllib.request.Request(API, headers=HEADERS)
    opener = opener or urllib.request.urlopen
    with opener(request, timeout=10) as response:
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('GitHub release metadata is unexpectedly large.')
    meta = json.loads(raw.decode('utf-8'))
    if meta.get('draft') or meta.get('prerelease'):
        raise ValueError('Only stable, published GitHub releases are supported.')
    tag = str(meta.get('tag_name', ''))
    if not VERSION_RE.fullmatch(tag):
        raise ValueError(f'Unexpected GitHub release version: {tag}')
    name = ASSETS[platform]
    matches = [a for a in meta.get('assets', []) if a.get('name') == name]
    if len(matches) != 1:
        raise ValueError(f'The GitHub release does not contain exactly one {name}.')
    asset = matches[0]
    url = _https(str(asset.get('browser_download_url', '')))
    parsed = urllib.parse.urlparse(url)
    expected_path = f'/{REPO}/releases/download/{tag}/{name}'
    if parsed.hostname != 'github.com' or parsed.path != expected_path:
        raise ValueError('Release asset URL does not match the official repository.')
    digest = str(asset.get('digest') or '').lower()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('This GitHub asset lacks the required SHA-256 checksum.')
    page = str(meta.get('html_url', ''))
    if page != f'https://github.com/{REPO}/releases/tag/{tag}':
        raise ValueError('Unexpected GitHub release page URL.')
    size = int(asset.get('size') or 0)
    if not 0 < size < 40 * 1024 * 1024:
        raise ValueError('Unexpected release archive size.')
    return {
        'tag': tag, 'url': url, 'page': page, 'digest': digest.removeprefix('sha256:'),
        'asset_name': name, 'size': size, 'platform': platform,
        'notes': str(meta.get('body') or '')[:4500],
    }


def is_current_release(install_root, release):
    installed = local_revision(install_root)
    return bool(installed and installed[:8].lower() == release['tag'].rsplit('-', 1)[-1])


def download_verified(release, dest, progress=None, opener=None):
    """Download a single official archive and verify the GitHub-published digest."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = _https(release['url'])
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(url, headers={'User-Agent': HEADERS['User-Agent']})
    count = 0
    hash256 = hashlib.sha256()
    try:
        with opener(request, timeout=25) as response, dest.open('wb') as output:
            if hasattr(response, 'geturl'):
                _https(response.geturl())  # HTTPS must survive redirects.
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > 40 * 1024 * 1024:
                    raise ValueError('The downloaded archive exceeds the size limit.')
                hash256.update(chunk)
                output.write(chunk)
                if progress:
                    progress(count, release['size'])
        if count != release['size']:
            raise ValueError('The download size does not match GitHub.')
        if hash256.hexdigest() != release['digest']:
            raise ValueError('SHA-256 verification failed; no files have been changed.')
        with zipfile.ZipFile(dest) as archive:
            if archive.testzip() is not None:
                raise ValueError('The downloaded ZIP is damaged.')
        return dest
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


def _safe_entry(info):
    name = info.filename
    if info.is_dir():
        return None
    path = PurePosixPath(name)
    raw_segments = name.split('/')
    if (not name or any(part in ('', '.', '..') for part in raw_segments) or '\\' in name or path.is_absolute() or
            len(path.parts) == 0 or any(p in ('', '.', '..') for p in path.parts)):
        raise ValueError(f'Unsafe archive path: {name!r}')
    mode = (info.external_attr >> 16) & 0o170000
    if mode not in (0, stat.S_IFREG):
        raise ValueError(f'Archive contains a symlink or other special file: {name}')
    if info.file_size > MAX_ENTRY_SIZE:
        raise ValueError(f'Archive contains an oversized file: {name}')
    # Only application code/assets in our own ZIP may be replaced. Never touch
    # saved sessions, OCR models, path files, caches or configuration.
    if len(path.parts) == 2 and path.parts[0] == '_internal':
        if path.name in {'config.json', 'update_result.json'}:
            return None
        allowed = ('.py', '.pyw', '.txt')
        if path.suffix not in allowed:
            raise ValueError(f'Unexpected internal file: {name}')
        if path.name in {'python_path.txt', 'pythonw_path.txt', 'LATEST_SESSION.txt'}:
            return None
        return path
    allowed_root = {
        'README.txt', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'VERSION.txt',
        'START DDO Combat Analyzer.cmd', 'SETUP DDO Combat Analyzer.cmd',
        'START_DDO_COMBAT_ANALYZER.sh', 'SETUP DDO Combat Analyzer.sh',
    }
    if len(path.parts) == 1 and path.name in allowed_root:
        return path
    raise ValueError(f'Unexpected file in the release archive: {name}')


def prepare_update(archive_path, staging_root, release):
    """Extract only approved code. Validate everything before touching the installation."""
    staging_root = Path(staging_root)
    staging_root.mkdir(parents=True, exist_ok=True)
    extracted = []
    total = 0
    with zipfile.ZipFile(archive_path) as archive:
        items = archive.infolist()
        if len(items) > MAX_ENTRIES:
            raise ValueError('Too many ZIP entries.')
        names = set()
        for info in items:
            path = _safe_entry(info)
            if path is None:
                continue
            if str(path).lower() in names:
                raise ValueError(f'Duplicate file in ZIP: {path}')
            names.add(str(path).lower())
            total += info.file_size
            if total > MAX_TOTAL_SIZE:
                raise ValueError('The ZIP expands beyond the update size limit.')
            extracted.append((info, path))
        required = {'_internal/DDO Combat Analyzer.pyw',
                    '_internal/update_manager.py', '_internal/launch_gui.py',
                    'VERSION.txt'}
        if release['platform'] == 'windows':
            required.add('START DDO Combat Analyzer.cmd')
        else:
            required.add('START_DDO_COMBAT_ANALYZER.sh')
        missing = {p for p in required if p.lower() not in names}
        if missing:
            raise ValueError(f'Release is missing required files: {sorted(missing)}')
        version_txt = archive.read('VERSION.txt').decode('utf-8')
        rev = re.search(r'^Source commit:\s*([0-9a-f]{40})\s*$', version_txt, re.M)
        if not rev or rev.group(1)[:8] != release['tag'].rsplit('-', 1)[-1]:
            raise ValueError('Version inside the archive does not match the release tag.')
        for info, path in extracted:
            target = staging_root.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open('wb') as dest:
                shutil.copyfileobj(source, dest, length=128 * 1024)
            if path.suffix == '.sh':
                target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return [path for _, path in extracted]


def _contained(install_root, path):
    root = Path(install_root).resolve()
    dest = (root / path).resolve()
    if root not in dest.parents:
        raise ValueError('Update tried to access outside the installation folder.')
    return dest


def install_staged(install_root, staging_root, paths, backup_dir):
    """Transactional replacement: restore originals if any file operation fails."""
    root = Path(install_root).resolve()
    stage = Path(staging_root).resolve()
    backup = Path(backup_dir).resolve()
    backup.mkdir(parents=True, exist_ok=True)
    processed = []
    try:
        for path in paths:
            dest = _contained(root, path)
            new_file = stage.joinpath(*path.parts)
            old_file = backup.joinpath(*path.parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            old_file.parent.mkdir(parents=True, exist_ok=True)
            existed = dest.exists()
            if existed:
                if not dest.is_file():
                    raise ValueError(f'Refusing to replace non-file: {dest}')
                shutil.copy2(dest, old_file)
            # Keep rollback metadata even if os.replace itself fails.
            processed.append((dest, old_file, existed))
            os.replace(new_file, dest)
        return True
    except BaseException:
        failures = []
        for dest, old_file, existed in reversed(processed):
            try:
                if existed:
                    shutil.copy2(old_file, dest)
                else:
                    dest.unlink(missing_ok=True)
            except Exception as exc:
                failures.append(f'{dest.name}: {exc}')
        if failures:
            raise RuntimeError('Update failed; rollback was incomplete: ' + '; '.join(failures))
        raise


def _parent_has_exited(pid):
    if not pid:
        return True
    if os.name == 'nt':
        import ctypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        SYNCHRONIZE = 0x00100000
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not handle:
            return True
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False


def _launch_program(root, platform):
    root = Path(root)
    if platform == 'windows':
        script = root / 'START DDO Combat Analyzer.cmd'
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        subprocess.Popen(['cmd.exe', '/c', str(script)], cwd=str(root),
                         creationflags=flags, close_fds=True)
    else:
        script = root / 'START_DDO_COMBAT_ANALYZER.sh'
        subprocess.Popen(['bash', str(script)], cwd=str(root),
                         start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)


def apply_external(install_root, archive_path, release, parent_pid, report=None):
    root = Path(install_root).resolve()
    archive_path = Path(archive_path).resolve()
    if not root.is_dir():
        raise ValueError('Installation directory does not exist.')
    if release['platform'] != platform_key():
        raise ValueError('This release is for another operating system.')
    if not archive_path.is_file():
        raise FileNotFoundError('Prepared release ZIP was not found.')
    # The parent must fully exit to release old Python modules and other files.
    for _ in range(300):
        if _parent_has_exited(parent_pid):
            break
        time.sleep(0.2)
    else:
        raise TimeoutError('The old program did not close; update cancelled safely.')
    temp_root = Path(tempfile.mkdtemp(prefix='.ddo-update-', dir=root))
    backup_dir = temp_root / 'backup'
    completed = False
    installed = False
    try:
        staging = temp_root / 'unpacked'
        # Verify a second time, even if the GUI already checked the download.
        h = hashlib.sha256()
        with archive_path.open('rb') as stream:
            for block in iter(lambda: stream.read(256 * 1024), b''):
                h.update(block)
        if h.hexdigest() != release['digest']:
            raise ValueError('The staged update checksum has changed.')
        paths = prepare_update(archive_path, staging, release)
        install_staged(root, staging, paths, backup_dir)
        installed = True
        _launch_program(root, release['platform'])
        # Only after a restart has been launched do we mark the release installed.
        (root / 'LOCAL_TEST_VERSION.txt').unlink(missing_ok=True)
        result = root / '_internal' / 'update_result.json'
        result.write_text(json.dumps({'tag': release['tag'], 'success': True}, indent=2), encoding='utf-8')
        completed = True
    except BaseException:
        if installed:
            # Launcher failure: restore the previous program before returning.
            for path in reversed(paths):
                target = _contained(root, path)
                original = backup_dir.joinpath(*path.parts)
                if original.is_file():
                    shutil.copy2(original, target)
                else:
                    target.unlink(missing_ok=True)
        raise
    finally:
        # On failure, preserve backups for inspection. On success remove only
        # the update scratch directory, never user files or sessions.
        if completed:
            shutil.rmtree(temp_root, ignore_errors=True)
            archive_path.unlink(missing_ok=True)
            try:
                archive_path.parent.rmdir()
            except OSError:
                pass
    return True


class UpdateController:
    """Tk owner; worker threads communicate with GUI solely via a queue."""
    def __init__(self, app, button, install_root):
        import queue
        self.app = app
        self.button = button
        self.root = Path(install_root).resolve()
        self.messages = queue.Queue()
        self.busy = False
        self.release = None
        self._window = None
        self._download_window = None
        self.app.after(200, self._poll)
        # Never automatically replace local diagnostic/development builds.
        self.app.after(2200, lambda: self.check(manual=False))

    def check(self, manual=True):
        if self.busy:
            return
        self.busy = True
        self.button.config(state='disabled')
        if manual:
            self.button.config(text='Checking...')
        def job():
            try:
                self.messages.put(('check_ok', fetch_latest_release(), manual))
            except Exception as exc:
                self.messages.put(('check_error', str(exc), manual))
        threading.Thread(target=job, daemon=True, name='github-release-check').start()

    def _poll(self):
        from tkinter import messagebox
        try:
            while True:
                kind, value, extra = self.messages.get_nowait()
                if kind == 'check_ok':
                    self.busy = False
                    self.release = value
                    self.button.config(state='normal')
                    if is_current_release(self.root, value):
                        self.button.config(text='Check for updates')
                        if extra:
                            messagebox.showinfo('Check for updates',
                                f'You already have the latest release ({value["tag"]}).', parent=self.app)
                    elif is_test_install(self.root) and not extra:
                        # Development snapshots are not silently replaced or
                        # auto-nagged with the older official stable release.
                        self.button.config(text='Check for updates')
                    else:
                        self.button.config(text='Update available')
                        self._offer(value)
                elif kind == 'check_error':
                    self.busy = False
                    self.button.config(state='normal', text='Check for updates')
                    if extra:
                        messagebox.showwarning('Check for updates',
                            'Could not check GitHub right now. Your current installation is unaffected.\n\n'
                            + value, parent=self.app)
                elif kind == 'progress':
                    if self._download_window and self._download_window.winfo_exists():
                        self._download_window.progress['value'] = value
                        self._download_window.status.config(text=extra)
                elif kind == 'download_ok':
                    self.busy = False
                    self._finish_download(value, extra)
                elif kind == 'download_error':
                    self.busy = False
                    if self._download_window and self._download_window.winfo_exists():
                        self._download_window.destroy()
                    self._download_window = None
                    self.button.config(state='normal', text='Update available')
                    messagebox.showerror('Update failed',
                        'The update could not be prepared. No files were changed.\n\n' + value,
                        parent=self.app)
        except __import__('queue').Empty:
            pass
        try:
            if self.app.winfo_exists():
                self.app.after(200, self._poll)
        except Exception:
            pass

    def _offer(self, release):
        import tkinter as tk
        from tkinter import ttk
        if self._window and self._window.winfo_exists():
            self._window.lift()
            return
        win = tk.Toplevel(self.app)
        win.title('DDO Combat Analyzer - Update available')
        win.geometry('580x370')
        win.minsize(480, 300)
        win.transient(self.app)
        self._window = win
        frame = ttk.Frame(win, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='A new stable version is available',
                  font=('Segoe UI', 13, 'bold')).pack(anchor='w')
        revision = local_revision(self.root)
        local_label = revision[:8] if revision else 'Local test / unknown revision'
        ttk.Label(frame, text=f'Installed: {local_label}    GitHub release: {release["tag"]}').pack(anchor='w', pady=(10, 4))
        if is_test_install(self.root):
            ttk.Label(frame, text='This installation is a local test build. Updating will replace its program files.',
                      wraplength=530).pack(anchor='w', pady=(0, 8))
        notes = tk.Text(frame, height=9, wrap='word')
        notes.insert('1.0', (release['notes'] or 'No release notes provided.').strip())
        notes.config(state='disabled')
        notes.pack(fill='both', expand=True, pady=(0, 12))
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x')
        ttk.Button(buttons, text='Update now', command=lambda: self._start_download(win, release)).pack(side='left')
        ttk.Button(buttons, text='Manual download',
                   command=lambda: webbrowser.open(release['page'])).pack(side='left', padx=(8, 0))
        ttk.Button(buttons, text='Later', command=win.destroy).pack(side='right')

    def _start_download(self, win, release):
        import tkinter as tk
        from tkinter import messagebox, ttk
        if self.busy:
            return
        if self.app.proc is not None or self.app.recording_session is not None or getattr(self.app, 'analysis_background', False):
            messagebox.showwarning('Update later',
                'Stop recording and wait until all analyses are finished before updating.', parent=win)
            return
        win.destroy()
        dialog = tk.Toplevel(self.app)
        dialog.title('Downloading DDO Combat Analyzer update')
        dialog.geometry('520x155')
        dialog.transient(self.app)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text=f'Downloading and verifying {release["tag"]}').pack(anchor='w')
        progress = ttk.Progressbar(body, maximum=100, mode='determinate')
        progress.pack(fill='x', pady=(12, 8))
        status = ttk.Label(body, text='Connecting to GitHub...')
        status.pack(anchor='w')
        dialog.progress = progress
        dialog.status = status
        dialog.protocol('WM_DELETE_WINDOW', lambda: None)
        self._download_window = dialog
        self.busy = True
        self.button.config(state='disabled', text='Downloading update...')
        def job():
            parent = None
            try:
                parent = Path(tempfile.mkdtemp(prefix='.ddo-download-', dir=self.root))
                archive = parent / release['asset_name']
                def report(n, total):
                    pct = int(100 * n / max(total, 1))
                    self.messages.put(('progress', min(100, pct), f'{n:,} / {total:,} bytes'))
                download_verified(release, archive, progress=report)
                # Validate the archive structure before stopping the GUI.
                prep = parent / 'validate'
                prepare_update(archive, prep, release)
                shutil.rmtree(prep)
                self.messages.put(('download_ok', archive, release))
            except Exception as exc:
                if parent:
                    shutil.rmtree(parent, ignore_errors=True)
                self.messages.put(('download_error', str(exc), None))
        threading.Thread(target=job, daemon=True, name='github-release-download').start()

    def _finish_download(self, archive, release):
        from tkinter import messagebox
        if self._download_window and self._download_window.winfo_exists():
            self._download_window.destroy()
        self._download_window = None
        if self.app.proc is not None or self.app.recording_session is not None:
            messagebox.showwarning('Update later', 'A session started while downloading. Please try again when idle.', parent=self.app)
            shutil.rmtree(Path(archive).parent, ignore_errors=True)
            self.button.config(state='normal', text='Update available')
            return
        if not messagebox.askyesno('Install verified update',
            f'{release["tag"]} was downloaded and its checksum verified.\n\n'
            'The application will now close, install the update, and restart.\n'
            'Your settings, OCR models and saved combat sessions will be preserved.\n\n'
            'Continue?', parent=self.app):
            shutil.rmtree(Path(archive).parent, ignore_errors=True)
            self.button.config(state='normal', text='Update available')
            return
        try:
            helper = Path(__file__).resolve()
            cmd = [sys.executable, str(helper), '--apply', '--root', str(self.root),
                   '--archive', str(archive), '--release-json', json.dumps(release),
                   '--parent-pid', str(os.getpid())]
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            subprocess.Popen(cmd, cwd=str(self.root),
                creationflags=flags if os.name == 'nt' else 0,
                start_new_session=(os.name != 'nt'),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True)
            self.app.on_close()
        except Exception as exc:
            self.button.config(state='normal', text='Update available')
            messagebox.showerror('Update failed to start', str(exc), parent=self.app)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--root')
    parser.add_argument('--archive')
    parser.add_argument('--release-json')
    parser.add_argument('--parent-pid', type=int, default=0)
    args = parser.parse_args()
    if not args.apply:
        parser.error('Use --apply only as an external update helper.')
    root = Path(args.root).resolve()
    log = root / 'update_error.log'
    try:
        data = json.loads(args.release_json)
        apply_external(root, args.archive, data, args.parent_pid)
    except Exception:
        details = traceback.format_exc()
        try:
            log.write_text(details, encoding='utf-8')
        except Exception:
            pass
        try:
            import tkinter as tk
            from tkinter import messagebox
            window = tk.Tk(); window.withdraw()
            messagebox.showerror('DDO Combat Analyzer update failed',
                 'Update failed. The old program files were restored where possible.\n\n'
                 'See update_error.log in the installation folder.', parent=window)
            window.destroy()
        except Exception:
            pass
        sys.exit(1)


if __name__ == '__main__':
    main()
