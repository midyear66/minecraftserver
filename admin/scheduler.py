"""
Scheduled Task Manager for MC Server Manager.

Uses APScheduler BackgroundScheduler to run cron-based and preset-based
scheduled tasks: version checks, server restarts, commands, and broadcasts.
"""

import atexit
import json
import smtplib
import string
import random
import threading
import time
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# Module-level scheduler instance
_scheduler = None
_config_lock = threading.Lock()

# References to app functions (set during init)
_load_config = None
_save_config = None
_get_container_status = None
_send_mc_command = None
_stop_mc_container = None
_start_mc_container = None
_recreate_mc_container = None
_get_versions_for_type = None

# Schedule presets mapped to cron expressions
SCHEDULE_PRESETS = {
    'every_30min':     {'label': 'Every 30 minutes',     'cron': '*/30 * * * *'},
    'hourly':          {'label': 'Every hour',            'cron': '0 * * * *'},
    'every_6h':        {'label': 'Every 6 hours',         'cron': '0 */6 * * *'},
    'every_12h':       {'label': 'Every 12 hours',        'cron': '0 */12 * * *'},
    'daily_4am':       {'label': 'Daily at 4:00 AM',      'cron': '0 4 * * *'},
    'daily_noon':      {'label': 'Daily at 12:00 PM',     'cron': '0 12 * * *'},
    'weekly_sun_4am':  {'label': 'Weekly (Sunday 4 AM)',   'cron': '0 4 * * 0'},
}

TASK_TYPES = {
    'version_check': 'Version Check',
    'restart':       'Scheduled Restart',
    'command':       'Run Command',
    'broadcast':     'Broadcast Message',
}


def _generate_task_id():
    """Generate a unique task ID."""
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"task_{int(time.time())}_{suffix}"


def _get_cron_expr(task):
    """Get the cron expression string for a task."""
    if task['schedule_type'] == 'preset':
        preset = SCHEDULE_PRESETS.get(task['schedule_value'])
        if preset:
            return preset['cron']
        return None
    return task['schedule_value']


def validate_cron(expression):
    """Validate a 5-field cron expression. Returns True if valid."""
    try:
        CronTrigger.from_crontab(expression)
        return True
    except (ValueError, KeyError):
        return False


def _find_server_by_port(config, port):
    """Look up a server config entry by external port."""
    for srv in config.get('servers', []):
        if int(srv.get('external_port', 0)) == port:
            return srv
    return None


def _find_server_index_by_port(config, port):
    """Look up a server config entry index by external port."""
    for i, srv in enumerate(config.get('servers', [])):
        if int(srv.get('external_port', 0)) == port:
            return i
    return None


# --- Notification helper ---

def _send_notification(config, subject, body):
    """Send notification via all enabled channels (email and/or Pushover)."""
    notifications = config.get('notifications', {})

    # Email
    email_config = notifications.get('email', {})
    if email_config.get('enabled'):
        try:
            host = email_config.get('smtp_host', '')
            port = email_config.get('smtp_port', 587)
            tls = email_config.get('smtp_tls', True)
            user = email_config.get('smtp_user', '')
            password = email_config.get('smtp_password', '')
            from_address = email_config.get('from_address', '')
            to_addresses = email_config.get('to_addresses', [])

            if host and from_address and to_addresses:
                if tls:
                    server = smtplib.SMTP(host, port, timeout=10)
                    server.starttls()
                else:
                    server = smtplib.SMTP(host, port, timeout=10)

                if user and password:
                    server.login(user, password)

                msg = MIMEMultipart()
                msg['From'] = from_address
                msg['To'] = ', '.join(to_addresses)
                msg['Subject'] = subject
                msg.attach(MIMEText(body, 'plain'))

                server.sendmail(from_address, to_addresses, msg.as_string())
                server.quit()
                print(f"[Scheduler] Email sent: {subject}")
        except Exception as e:
            print(f"[Scheduler] Email error: {e}")

    # Pushover
    pushover_config = notifications.get('pushover', {})
    if pushover_config.get('enabled'):
        try:
            user_key = pushover_config.get('user_key', '')
            app_token = pushover_config.get('app_token', '')
            priority = pushover_config.get('priority', 0)

            if user_key and app_token:
                data = {
                    'token': app_token,
                    'user': user_key,
                    'title': subject,
                    'message': body,
                    'priority': priority,
                }
                requests.post(
                    'https://api.pushover.net/1/messages.json',
                    data=data, timeout=10,
                )
                print(f"[Scheduler] Pushover sent: {subject}")
        except Exception as e:
            print(f"[Scheduler] Pushover error: {e}")


# --- Task handlers ---

def _run_version_check(task, server, config):
    """Check for a newer server version. Notify or auto-update."""
    current_version = server.get('version', 'LATEST')
    server_type = server.get('type', 'VANILLA')
    server_name = server.get('name', 'Unknown')

    if current_version.upper() == 'LATEST':
        return "Skipped: server set to LATEST (always gets newest)"

    versions = _get_versions_for_type(server_type)
    if not versions:
        return f"Error: could not fetch version list for {server_type}"

    latest_version = versions[0]
    if latest_version == current_version:
        return f"No update available (current: {current_version})"

    action = task.get('config', {}).get('action', 'notify')

    if action == 'notify':
        _send_notification(
            config,
            f"[MC] Update Available: {server_name}",
            f"Server '{server_name}' can be updated from {current_version} to {latest_version}.",
        )
        return f"Update available: {current_version} -> {latest_version} (notification sent)"

    # auto_update
    container_name = server['container_name']
    was_running = _get_container_status(container_name) == 'running'

    # Update version in config
    with _config_lock:
        fresh_config = _load_config()
        idx = _find_server_index_by_port(fresh_config, int(server['external_port']))
        if idx is None:
            return "Error: server disappeared from config during update"
        fresh_config['servers'][idx]['version'] = latest_version
        _save_config(fresh_config)
        updated_server = fresh_config['servers'][idx]

    # Recreate container with new version
    success, _ = _recreate_mc_container(updated_server)
    if not success:
        return f"Error: failed to recreate container for update to {latest_version}"

    result = f"Updated: {current_version} -> {latest_version}"

    auto_restart = task.get('config', {}).get('auto_restart', True)
    if was_running and auto_restart:
        _start_mc_container(container_name)
        result += " (restarted)"
    elif was_running:
        result += " (was running, now stopped)"

    _send_notification(
        config,
        f"[MC] Server Updated: {server_name}",
        f"Server '{server_name}' updated from {current_version} to {latest_version}.",
    )
    return result


def _run_restart(task, server):
    """Stop and start a server container."""
    container_name = server['container_name']
    status = _get_container_status(container_name)

    if status != 'running':
        return "Skipped: server is not running"

    if not _stop_mc_container(container_name):
        return "Error: failed to stop server"

    # Brief pause to let the container fully stop
    time.sleep(2)

    if not _start_mc_container(container_name):
        return "Error: stopped but failed to restart"

    return "Server restarted"


def _run_command(task, server):
    """Send an arbitrary command to a running server."""
    container_name = server['container_name']
    command = task.get('config', {}).get('command', '')

    if not command:
        return "Error: no command configured"

    status = _get_container_status(container_name)
    if status != 'running':
        return "Skipped: server is not running"

    if _send_mc_command(container_name, command):
        return f"Command sent: {command}"
    return f"Error: failed to send command: {command}"


def _run_broadcast(task, server):
    """Send a broadcast message to a running server."""
    container_name = server['container_name']
    message = task.get('config', {}).get('message', '')

    if not message:
        return "Error: no message configured"

    status = _get_container_status(container_name)
    if status != 'running':
        return "Skipped: server is not running"

    if _send_mc_command(container_name, f"say {message}"):
        return f"Broadcast sent: {message}"
    return "Error: failed to send broadcast"


# --- Task execution dispatch ---

def _execute_task(task_id):
    """Main dispatch: called by APScheduler for each job."""
    with _config_lock:
        config = _load_config()

    task = None
    for t in config.get('scheduled_tasks', []):
        if t['id'] == task_id:
            task = t
            break

    if not task:
        print(f"[Scheduler] Task {task_id} not found in config")
        return

    server = _find_server_by_port(config, task['server_port'])
    if not server:
        result = f"Error: server not found (port {task['server_port']})"
    else:
        task_type = task['type']
        try:
            if task_type == 'version_check':
                result = _run_version_check(task, server, config)
            elif task_type == 'restart':
                result = _run_restart(task, server)
            elif task_type == 'command':
                result = _run_command(task, server)
            elif task_type == 'broadcast':
                result = _run_broadcast(task, server)
            else:
                result = f"Error: unknown task type '{task_type}'"
        except Exception as e:
            result = f"Error: {e}"

    print(f"[Scheduler] Task {task_id} ({task['type']}): {result}")

    # Update last_run and last_result in config
    with _config_lock:
        config = _load_config()
        for t in config.get('scheduled_tasks', []):
            if t['id'] == task_id:
                t['last_run'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                t['last_result'] = result
                break
        _save_config(config)


# --- Task registration ---

def _register_task(task):
    """Register a single task with the APScheduler."""
    if not task.get('enabled', False):
        return

    cron_expr = _get_cron_expr(task)
    if not cron_expr:
        print(f"[Scheduler] Invalid schedule for task {task['id']}")
        return

    try:
        trigger = CronTrigger.from_crontab(cron_expr)
        _scheduler.add_job(
            _execute_task,
            trigger,
            id=task['id'],
            args=[task['id']],
            replace_existing=True,
            misfire_grace_time=60,
        )
    except Exception as e:
        print(f"[Scheduler] Error registering task {task['id']}: {e}")


def _unregister_task(task_id):
    """Remove a task from the APScheduler."""
    try:
        _scheduler.remove_job(task_id)
    except Exception:
        pass  # Job may not exist


# --- CRUD operations ---

def get_all_tasks():
    """Return all scheduled tasks from config."""
    config = _load_config()
    return config.get('scheduled_tasks', [])


def add_task(task_dict):
    """Add a new task to config and register with scheduler."""
    task_dict['id'] = _generate_task_id()
    task_dict['last_run'] = None
    task_dict['last_result'] = None

    with _config_lock:
        config = _load_config()
        tasks = config.get('scheduled_tasks', [])
        tasks.append(task_dict)
        config['scheduled_tasks'] = tasks
        _save_config(config)

    if task_dict.get('enabled', False):
        _register_task(task_dict)

    print(f"[Scheduler] Added task {task_dict['id']} ({task_dict['type']})")
    return task_dict['id']


def remove_task(task_id):
    """Remove a task from config and unregister from scheduler."""
    _unregister_task(task_id)

    with _config_lock:
        config = _load_config()
        tasks = config.get('scheduled_tasks', [])
        config['scheduled_tasks'] = [t for t in tasks if t['id'] != task_id]
        _save_config(config)

    print(f"[Scheduler] Removed task {task_id}")


def toggle_task(task_id, enabled):
    """Enable or disable a task."""
    with _config_lock:
        config = _load_config()
        task = None
        for t in config.get('scheduled_tasks', []):
            if t['id'] == task_id:
                t['enabled'] = enabled
                task = t
                break
        _save_config(config)

    if task:
        if enabled:
            _register_task(task)
        else:
            _unregister_task(task_id)
        print(f"[Scheduler] Task {task_id} {'enabled' if enabled else 'disabled'}")


def run_task_now(task_id):
    """Manually trigger a task in a background thread."""
    thread = threading.Thread(target=_execute_task, args=(task_id,), daemon=True)
    thread.start()


# --- Initialization ---

def init_scheduler(load_config_fn, save_config_fn, get_container_status_fn,
                   send_mc_command_fn, stop_mc_container_fn, start_mc_container_fn,
                   recreate_mc_container_fn, get_versions_for_type_fn):
    """Initialize the scheduler with function references and start it."""
    global _scheduler
    global _load_config, _save_config
    global _get_container_status, _send_mc_command
    global _stop_mc_container, _start_mc_container
    global _recreate_mc_container, _get_versions_for_type

    _load_config = load_config_fn
    _save_config = save_config_fn
    _get_container_status = get_container_status_fn
    _send_mc_command = send_mc_command_fn
    _stop_mc_container = stop_mc_container_fn
    _start_mc_container = start_mc_container_fn
    _recreate_mc_container = recreate_mc_container_fn
    _get_versions_for_type = get_versions_for_type_fn

    _scheduler = BackgroundScheduler(daemon=True)

    config = _load_config()
    tasks = config.get('scheduled_tasks', [])
    active_count = 0
    for task in tasks:
        if task.get('enabled', False):
            _register_task(task)
            active_count += 1

    _scheduler.start()
    atexit.register(lambda: _scheduler.shutdown(wait=False))

    print(f"[Scheduler] Initialized with {active_count} active task(s) out of {len(tasks)} total")
