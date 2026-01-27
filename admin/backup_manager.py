import os
import json
import tarfile
import time
import threading
from datetime import datetime

BACKUPS_DIR = '/backups'
MC_DATA_DIR = '/mc_data'

# Directories/files to exclude from backups (large, regenerated on start)
EXCLUDE_DIRS = {'libraries', 'versions', '.cache', 'logs', 'cache'}

# Thread-safe state
_backup_in_progress = {}
_scheduler_timers = {}


def _get_backup_dir(backup_name):
    path = os.path.join(BACKUPS_DIR, backup_name)
    os.makedirs(path, exist_ok=True)
    return path


def _get_metadata_path(backup_name):
    return os.path.join(_get_backup_dir(backup_name), 'backups.json')


def _load_metadata(backup_name):
    path = _get_metadata_path(backup_name)
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_metadata(backup_name, data):
    path = _get_metadata_path(backup_name)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def _human_size(nbytes):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def _make_exclude_filter(base_path):
    """Create a tarfile filter that excludes large regeneratable dirs/files."""
    def _filter(tarinfo):
        # Get path relative to the archive root
        # Archive structure: container_name/file_or_dir
        parts = tarinfo.name.split('/')
        if len(parts) > 1:
            top_level = parts[1]
        else:
            return tarinfo

        # Exclude known directories
        if top_level in EXCLUDE_DIRS:
            return None

        # Exclude jar files at top level only
        if len(parts) == 2 and top_level.endswith('.jar'):
            return None

        return tarinfo

    return _filter


def is_backup_in_progress(backup_name):
    return _backup_in_progress.get(backup_name, False)


def list_backups(backup_name):
    """Return list of backup metadata dicts, sorted newest-first."""
    metadata = _load_metadata(backup_name)
    backup_dir = _get_backup_dir(backup_name)

    # Reconcile: keep only entries whose files still exist
    valid = []
    for entry in metadata:
        filepath = os.path.join(backup_dir, entry['filename'])
        if os.path.isfile(filepath):
            entry['size_bytes'] = os.path.getsize(filepath)
            entry['size_human'] = _human_size(entry['size_bytes'])
            valid.append(entry)

    valid.sort(key=lambda e: e['filename'], reverse=True)

    # Save reconciled list if entries were removed
    if len(valid) != len(metadata):
        _save_metadata(backup_name, valid)

    return valid


def create_backup(backup_name, container_name, backup_type='manual',
                  send_mc_command_fn=None, get_status_fn=None):
    """
    Create a tar.gz backup of the server data directory.
    For running servers: sends save-all/save-off first, then save-on after.
    Returns (success, message, filename or None).
    """
    if _backup_in_progress.get(backup_name):
        return False, 'Backup already in progress', None

    _backup_in_progress[backup_name] = True
    filepath = None

    try:
        data_path = os.path.join(MC_DATA_DIR, container_name)
        if not os.path.isdir(data_path):
            return False, 'Server data directory not found', None

        backup_dir = _get_backup_dir(backup_name)
        timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
        filename = f"{timestamp}.tar.gz"
        filepath = os.path.join(backup_dir, filename)

        # If server is running, pause saving for consistency
        is_running = False
        if get_status_fn and send_mc_command_fn:
            is_running = get_status_fn(container_name) == 'running'

        if is_running and send_mc_command_fn:
            send_mc_command_fn(container_name, 'save-all flush')
            time.sleep(3)
            send_mc_command_fn(container_name, 'save-off')
            time.sleep(1)

        try:
            with tarfile.open(filepath, 'w:gz') as tar:
                tar.add(data_path, arcname=container_name,
                        filter=_make_exclude_filter(data_path))
        finally:
            if is_running and send_mc_command_fn:
                send_mc_command_fn(container_name, 'save-on')

        size_bytes = os.path.getsize(filepath)
        metadata = _load_metadata(backup_name)
        metadata.append({
            'filename': filename,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'size_bytes': size_bytes,
            'size_human': _human_size(size_bytes),
            'type': backup_type,
        })
        _save_metadata(backup_name, metadata)

        return True, f'Backup created: {filename}', filename

    except Exception as e:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
        return False, f'Backup failed: {str(e)}', None

    finally:
        _backup_in_progress[backup_name] = False


def restore_backup(backup_name, container_name, filename, stop_fn, start_fn, get_status_fn):
    """
    Restore a backup. Stops server if running, extracts archive, restarts if needed.
    Returns (success, message).
    """
    if '..' in filename or '/' in filename:
        return False, 'Invalid backup filename'

    backup_dir = _get_backup_dir(backup_name)
    filepath = os.path.join(backup_dir, filename)

    if not os.path.isfile(filepath):
        return False, 'Backup file not found'

    was_running = get_status_fn(container_name) == 'running'

    if was_running:
        if not stop_fn(container_name):
            return False, 'Failed to stop server for restore'
        time.sleep(5)

    try:
        with tarfile.open(filepath, 'r:gz') as tar:
            for member in tar.getmembers():
                if member.name.startswith('/') or '..' in member.name:
                    return False, 'Backup archive contains unsafe paths'
            tar.extractall(path=MC_DATA_DIR)

        msg = 'Backup restored successfully.'

        if was_running:
            if start_fn(container_name):
                msg += ' Server restarted.'
            else:
                msg += ' WARNING: Failed to restart server.'

        return True, msg

    except Exception as e:
        if was_running:
            start_fn(container_name)
        return False, f'Restore failed: {str(e)}'


def delete_backup(backup_name, filename):
    """Delete a backup file and its metadata entry."""
    if '..' in filename or '/' in filename:
        return False, 'Invalid filename'

    backup_dir = _get_backup_dir(backup_name)
    filepath = os.path.join(backup_dir, filename)

    if os.path.isfile(filepath):
        os.remove(filepath)

    metadata = _load_metadata(backup_name)
    metadata = [e for e in metadata if e['filename'] != filename]
    _save_metadata(backup_name, metadata)

    return True, 'Backup deleted'


def get_backup_filepath(backup_name, filename):
    """Return the full path to a backup file, or None if invalid/missing."""
    if '..' in filename or '/' in filename:
        return None
    filepath = os.path.join(_get_backup_dir(backup_name), filename)
    if os.path.isfile(filepath):
        return filepath
    return None


def _prune_old_backups(backup_name, max_backups):
    """Remove oldest auto-backups if count exceeds max_backups."""
    metadata = _load_metadata(backup_name)
    auto_backups = [e for e in metadata if e.get('type') == 'auto']
    auto_backups.sort(key=lambda e: e['filename'])

    while len(auto_backups) > max_backups:
        oldest = auto_backups.pop(0)
        delete_backup(backup_name, oldest['filename'])


# --- Auto-backup scheduler ---

def _auto_backup_callback(backup_name, container_name, interval_hours, max_backups,
                          send_mc_command_fn, get_status_fn):
    """Called by the scheduler timer. Creates backup, prunes, reschedules."""
    print(f"[Auto-backup] Starting backup for {backup_name}")

    success, msg, _ = create_backup(
        backup_name,
        container_name,
        backup_type='auto',
        send_mc_command_fn=send_mc_command_fn,
        get_status_fn=get_status_fn,
    )

    if success:
        _prune_old_backups(backup_name, max_backups)
        print(f"[Auto-backup] Completed for {backup_name}: {msg}")
    else:
        print(f"[Auto-backup] Failed for {backup_name}: {msg}")

    # Reschedule next run
    schedule_auto_backup(backup_name, container_name, interval_hours, max_backups,
                         send_mc_command_fn, get_status_fn)


def schedule_auto_backup(backup_name, container_name, interval_hours, max_backups,
                         send_mc_command_fn, get_status_fn):
    """Schedule (or reschedule) the next auto-backup timer for a server."""
    cancel_auto_backup(backup_name)

    interval_seconds = interval_hours * 3600
    timer = threading.Timer(
        interval_seconds,
        _auto_backup_callback,
        args=(backup_name, container_name, interval_hours, max_backups,
              send_mc_command_fn, get_status_fn)
    )
    timer.daemon = True
    timer.start()
    _scheduler_timers[backup_name] = timer
    print(f"[Auto-backup] Scheduled for {backup_name} in {interval_hours}h")


def cancel_auto_backup(backup_name):
    """Cancel any pending auto-backup timer for a server."""
    timer = _scheduler_timers.pop(backup_name, None)
    if timer:
        timer.cancel()


def init_auto_backups(config, get_backup_dir_name_fn, send_mc_command_fn, get_status_fn):
    """Initialize auto-backup timers for all servers on app startup."""
    for srv in config.get('servers', []):
        settings = srv.get('backup_settings', {})
        if settings.get('auto_enabled', False):
            backup_name = get_backup_dir_name_fn(srv, config)
            schedule_auto_backup(
                backup_name,
                srv['container_name'],
                settings.get('interval_hours', 6),
                settings.get('max_backups', 5),
                send_mc_command_fn,
                get_status_fn,
            )
