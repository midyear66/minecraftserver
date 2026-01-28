import os
import json
import re
import time
import functools
import threading
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, send_file
from werkzeug.utils import secure_filename
import glob as glob_module
from dotenv import load_dotenv
import docker
import requests
import backup_manager
import scheduler

load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max upload

# Configuration
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'changeme')
CONFIG_PATH = '/config/config.json'
LOGS_DIR = '/app/logs'
PROXY_CONTAINER_NAME = 'mc_proxy'
HOST_DATA_DIR = os.getenv('HOST_DATA_DIR', '/home/sanford/minecraftserver/mc_data')
MC_DATA_DIR = '/mc_data'

# Valid server types for itzg/minecraft-server
VALID_SERVER_TYPES = ['VANILLA', 'PAPER', 'SPIGOT', 'FABRIC', 'FORGE']

# Version cache
_version_cache = {}
VERSION_CACHE_TTL = 3600  # 1 hour
MAX_VERSIONS = 30

# Default env var values for itzg/minecraft-server
ENV_DEFAULTS = {
    'MOTD': 'A Minecraft Server',
    'MODE': 'survival',
    'DIFFICULTY': 'easy',
    'MAX_PLAYERS': 20,
    'PVP': True,
    'ONLINE_MODE': True,
    'SPAWN_PROTECTION': 16,
    'VIEW_DISTANCE': 10,
    'ALLOW_NETHER': True,
    'ENABLE_COMMAND_BLOCK': False,
    'SEED': '',
    'LEVEL_TYPE': 'default',
    'ICON': '',
    'USE_AIKAR_FLAGS': False,
    'SPIGET_RESOURCES': '',
}

BOOLEAN_ENV_VARS = {'PVP', 'ONLINE_MODE', 'ALLOW_NETHER', 'ENABLE_COMMAND_BLOCK', 'USE_AIKAR_FLAGS'}
INTEGER_ENV_VARS = {'MAX_PLAYERS', 'SPAWN_PROTECTION', 'VIEW_DISTANCE'}


def get_default_config():
    """Return the default configuration structure"""
    return {
        'timeout': 5,
        'auto_shutdown': True,
        'servers': [],
        'scheduled_tasks': [],
        'notifications': {
            'email': {
                'enabled': False,
                'smtp_host': '',
                'smtp_port': 587,
                'smtp_tls': True,
                'smtp_user': '',
                'smtp_password': '',
                'from_address': '',
                'to_addresses': [],
                'events': {
                    'server_start': True,
                    'server_stop': True,
                    'player_join': False,
                    'player_leave': False,
                    'unauthorized_login': False
                }
            },
            'pushover': {
                'enabled': False,
                'user_key': '',
                'app_token': '',
                'priority': 0,
                'events': {
                    'server_start': True,
                    'server_stop': True,
                    'player_join': False,
                    'player_leave': False,
                    'unauthorized_login': False
                }
            }
        }
    }


def load_config():
    """Load proxy configuration from config.json, creating default if missing"""
    try:
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        # Create default config file on first run
        default_config = get_default_config()
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, 'w') as f:
                json.dump(default_config, f, indent=2)
        except Exception as e:
            print(f"Warning: Could not create default config file: {e}")
        return default_config
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in config file: {e}")
        return get_default_config()


def save_config(config):
    """Save proxy configuration to config.json"""
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2)
        return True
    except Exception as e:
        print(f"Error saving config: {e}")
        raise


def restart_proxy():
    """Restart the mc_proxy container"""
    try:
        client = docker.from_env()
        container = client.containers.get(PROXY_CONTAINER_NAME)
        container.restart()
        return True
    except Exception as e:
        print(f"Error restarting proxy: {e}")
        return False


def sanitize_container_name(name):
    """Convert a server name to a valid Docker container name"""
    # Lowercase, replace spaces/special chars with underscores
    sanitized = re.sub(r'[^a-z0-9_.-]', '_', name.lower().strip())
    sanitized = re.sub(r'_+', '_', sanitized).strip('_')
    if not sanitized:
        sanitized = 'mc_server'
    # Prefix with mc_ if not already
    if not sanitized.startswith('mc_'):
        sanitized = 'mc_' + sanitized
    return sanitized


def sanitize_backup_name(name):
    """Convert a server name to a safe backup directory name."""
    sanitized = re.sub(r'[^\w\s-]', '', name).strip()
    sanitized = re.sub(r'[\s]+', '_', sanitized)
    sanitized = re.sub(r'_+', '_', sanitized).strip('_')
    return sanitized or 'server'


def get_backup_dir_name(server, config):
    """Get (or assign and persist) the backup directory name for a server."""
    if 'backup_dir_name' not in server:
        server['backup_dir_name'] = sanitize_backup_name(server['name'])
        save_config(config)
    return server['backup_dir_name']


def get_next_internal_port(config):
    """Get the next available internal port starting from 30001"""
    used_ports = {int(s['internal_port']) for s in config.get('servers', [])}
    port = 30001
    while port in used_ports:
        port += 1
    return port


def _fetch_mojang_versions():
    """Fetch release versions from Mojang's version manifest.
    Used for VANILLA, SPIGOT, and FORGE server types.
    """
    resp = requests.get(
        'https://piston-meta.mojang.com/mc/game/version_manifest_v2.json',
        timeout=10
    )
    resp.raise_for_status()
    data = resp.json()
    releases = [v['id'] for v in data['versions'] if v['type'] == 'release']
    return releases[:MAX_VERSIONS]


def _fetch_paper_versions():
    """Fetch stable versions from the PaperMC Fill v3 API."""
    resp = requests.get(
        'https://fill.papermc.io/v3/projects/paper',
        headers={'User-Agent': 'MCServerManager/1.0'},
        timeout=10
    )
    resp.raise_for_status()
    data = resp.json()
    version_groups = data.get('versions', {})
    # Flatten groups (already newest-first) and filter out pre-releases
    all_versions = []
    for group_versions in version_groups.values():
        for v in group_versions:
            if '-' not in v:
                all_versions.append(v)
    return all_versions[:MAX_VERSIONS]


def _fetch_fabric_versions():
    """Fetch stable versions from the Fabric Meta API."""
    resp = requests.get(
        'https://meta.fabricmc.net/v2/versions/game',
        timeout=10
    )
    resp.raise_for_status()
    data = resp.json()
    stable = [v['version'] for v in data if v.get('stable')]
    return stable[:MAX_VERSIONS]


def get_versions_for_type(server_type):
    """Get cached version list for a server type."""
    server_type = server_type.upper()

    cached = _version_cache.get(server_type)
    if cached and (time.time() - cached['fetched_at']) < VERSION_CACHE_TTL:
        return cached['versions']

    fetchers = {
        'VANILLA': _fetch_mojang_versions,
        'SPIGOT': _fetch_mojang_versions,
        'FORGE': _fetch_mojang_versions,
        'PAPER': _fetch_paper_versions,
        'FABRIC': _fetch_fabric_versions,
    }

    fetcher = fetchers.get(server_type)
    if not fetcher:
        return []

    try:
        versions = fetcher()
        _version_cache[server_type] = {
            'versions': versions,
            'fetched_at': time.time(),
        }
        return versions
    except Exception as e:
        print(f"Error fetching versions for {server_type}: {e}")
        if cached:
            return cached['versions']
        return []


def get_docker_client():
    """Get a Docker client"""
    return docker.from_env()


def get_container_status(container_name):
    """Get the status of a Docker container"""
    try:
        client = get_docker_client()
        container = client.containers.get(container_name)
        return container.status  # 'running', 'exited', 'created', etc.
    except docker.errors.NotFound:
        return 'not_found'
    except Exception as e:
        print(f"Error getting container status for {container_name}: {e}")
        return 'unknown'


def create_mc_container(server_config):
    """Create a Docker container for a Minecraft server (stopped)"""
    client = get_docker_client()
    container_name = server_config['container_name']
    internal_port = int(server_config['internal_port'])

    # Host path for data (HOST_DATA_DIR must be absolute path on host)
    data_path = os.path.join(HOST_DATA_DIR, container_name)

    # Environment variables for itzg/minecraft-server
    environment = {
        'EULA': 'TRUE',
        'TYPE': server_config.get('type', 'VANILLA'),
        'VERSION': server_config.get('version', 'LATEST'),
        'MEMORY': server_config.get('memory', '2G'),
        'ENABLE_QUERY': 'false',
        'ENABLE_RCON': 'false',
    }

    # Merge custom env vars from config (set via edit page)
    custom_env = server_config.get('env', {})
    for key, value in custom_env.items():
        if isinstance(value, bool):
            environment[key] = 'true' if value else 'false'
        else:
            environment[key] = str(value)

    # Pull image if needed
    try:
        client.images.get('itzg/minecraft-server:latest')
    except docker.errors.ImageNotFound:
        print(f"Pulling itzg/minecraft-server:latest...")
        client.images.pull('itzg/minecraft-server', tag='latest')

    # Create container
    container = client.containers.create(
        'itzg/minecraft-server:latest',
        name=container_name,
        environment=environment,
        ports={'25565/tcp': ('127.0.0.1', internal_port)},
        volumes={
            data_path: {'bind': '/data', 'mode': 'rw'}
        },
        labels={'managed_by': 'mc_manager'},
        restart_policy={'Name': 'no'},
        detach=True,
        stdin_open=True,
        tty=True,
    )
    return container


def delete_mc_container(container_name):
    """Delete a Docker container (data dir preserved)"""
    try:
        client = get_docker_client()
        container = client.containers.get(container_name)
        # Stop first if running
        if container.status == 'running':
            container.stop(timeout=30)
        container.remove()
        return True
    except docker.errors.NotFound:
        return True  # Already gone
    except Exception as e:
        print(f"Error deleting container {container_name}: {e}")
        return False


def start_mc_container(container_name):
    """Start a Docker container"""
    try:
        client = get_docker_client()
        container = client.containers.get(container_name)
        container.start()
        return True
    except Exception as e:
        print(f"Error starting container {container_name}: {e}")
        return False


def stop_mc_container(container_name):
    """Stop a Docker container"""
    try:
        client = get_docker_client()
        container = client.containers.get(container_name)
        container.stop(timeout=30)
        return True
    except Exception as e:
        print(f"Error stopping container {container_name}: {e}")
        return False


def recreate_mc_container(server_config):
    """Recreate a container with updated environment (data preserved).

    Returns (success: bool, was_running: bool)
    """
    container_name = server_config['container_name']
    was_running = get_container_status(container_name) == 'running'

    if not delete_mc_container(container_name):
        return False, was_running

    try:
        create_mc_container(server_config)
        return True, was_running
    except Exception as e:
        print(f"Error recreating container {container_name}: {e}")
        return False, was_running


ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\r')

PLAYER_LIST_CONFIG = {
    'whitelist': {
        'filename': 'whitelist.json',
        'add_cmd': 'whitelist add {name}',
        'remove_cmd': 'whitelist remove {name}',
    },
    'banned': {
        'filename': 'banned-players.json',
        'add_cmd': 'ban {name}',
        'remove_cmd': 'pardon {name}',
    },
    'ops': {
        'filename': 'ops.json',
        'add_cmd': 'op {name}',
        'remove_cmd': 'deop {name}',
    },
}


def strip_ansi(text):
    """Remove ANSI escape codes from text."""
    return ANSI_ESCAPE.sub('', text)


def send_mc_command(container_name, command):
    """Send a command to a running Minecraft server's stdin."""
    try:
        client = get_docker_client()
        container = client.containers.get(container_name)
        if container.status != 'running':
            return False
        sock = container.attach_socket(params={'stdin': 1, 'stream': 1})
        sock._sock.sendall((command + '\n').encode('utf-8'))
        sock.close()
        return True
    except Exception as e:
        print(f"Error sending command to {container_name}: {e}")
        return False


def get_server_by_port(port):
    """Look up a server config entry by its external port."""
    config = load_config()
    for srv in config.get('servers', []):
        if int(srv.get('external_port', 0)) == port:
            return srv, config
    return None, config


def lookup_mojang_uuid(username):
    """Look up a Minecraft player's UUID from the Mojang API."""
    try:
        resp = requests.get(
            f'https://api.mojang.com/users/profiles/minecraft/{username}',
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            raw_uuid = data['id']
            formatted = f'{raw_uuid[:8]}-{raw_uuid[8:12]}-{raw_uuid[12:16]}-{raw_uuid[16:20]}-{raw_uuid[20:]}'
            return formatted, data['name']
        return None, None
    except Exception as e:
        print(f"Mojang API error for {username}: {e}")
        return None, None


def read_player_json(container_name, filename):
    """Read a player management JSON file from the server data directory."""
    filepath = os.path.join(MC_DATA_DIR, container_name, filename)
    try:
        with open(filepath, 'r') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    except Exception as e:
        print(f"Error reading {filepath}: {e}")
        return []


def write_player_json(container_name, filename, data):
    """Write a player management JSON file to the server data directory."""
    filepath = os.path.join(MC_DATA_DIR, container_name, filename)
    try:
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        print(f"Error writing {filepath}: {e}")
        return False


def read_server_property(container_name, key):
    """Read a single property from server.properties."""
    filepath = os.path.join(MC_DATA_DIR, container_name, 'server.properties')
    try:
        with open(filepath, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                if k.strip() == key:
                    return v.strip()
    except Exception as e:
        print(f"Error reading server.properties for {container_name}: {e}")
    return None


def write_server_property(container_name, key, value):
    """Update a single property in server.properties."""
    filepath = os.path.join(MC_DATA_DIR, container_name, 'server.properties')
    try:
        lines = []
        found = False
        with open(filepath, 'r') as f:
            for line in f:
                stripped = line.strip()
                if not stripped.startswith('#') and '=' in stripped:
                    k, _ = stripped.split('=', 1)
                    if k.strip() == key:
                        lines.append(f'{key}={value}\n')
                        found = True
                        continue
                lines.append(line)
        if not found:
            lines.append(f'{key}={value}\n')
        with open(filepath, 'w') as f:
            f.writelines(lines)
        return True
    except Exception as e:
        print(f"Error writing server.properties for {container_name}: {e}")
        return False


def login_required(f):
    """Decorator to require login for routes"""
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            # Return JSON error for API endpoints
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid credentials', 'error')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))


@app.route('/')
@login_required
def dashboard():
    config = load_config()

    # Build server info with status from Docker
    servers = []
    for srv in config.get('servers', []):
        container_name = srv.get('container_name', '')
        status = get_container_status(container_name)

        server_info = {
            'name': srv.get('name', container_name),
            'container_name': container_name,
            'external_port': srv.get('external_port'),
            'internal_port': srv.get('internal_port'),
            'type': srv.get('type', 'VANILLA'),
            'version': srv.get('version', 'LATEST'),
            'memory': srv.get('memory', '2G'),
            'running': status == 'running',
            'status': status,
        }
        servers.append(server_info)

    servers.sort(key=lambda s: s['name'].lower())

    return render_template('dashboard.html',
                         servers=servers,
                         config=config,
                         server_types=VALID_SERVER_TYPES)


@app.route('/servers/create', methods=['POST'])
@login_required
def create_server():
    name = request.form.get('name', '').strip()
    port = request.form.get('port', '').strip()
    server_type = request.form.get('type', 'VANILLA').upper()
    version = request.form.get('version', 'LATEST').strip()
    memory = request.form.get('memory', '2G').strip()

    if not name:
        flash('Server name is required', 'error')
        return redirect(url_for('dashboard'))

    if not port:
        flash('External port is required', 'error')
        return redirect(url_for('dashboard'))

    try:
        port = int(port)
        if port < 1 or port > 65535:
            raise ValueError
    except ValueError:
        flash('Port must be a number between 1 and 65535', 'error')
        return redirect(url_for('dashboard'))

    if server_type not in VALID_SERVER_TYPES:
        flash(f'Invalid server type: {server_type}', 'error')
        return redirect(url_for('dashboard'))

    if not version:
        version = 'LATEST'

    config = load_config()

    # Check if port already exists
    for srv in config.get('servers', []):
        if int(srv.get('external_port', 0)) == port:
            flash(f'Port {port} already in use by "{srv["name"]}"', 'error')
            return redirect(url_for('dashboard'))

    # Generate container name and internal port
    container_name = sanitize_container_name(name)

    # Ensure container name is unique
    existing_names = {s['container_name'] for s in config.get('servers', [])}
    base_name = container_name
    counter = 2
    while container_name in existing_names:
        container_name = f'{base_name}_{counter}'
        counter += 1

    internal_port = get_next_internal_port(config)

    server_config = {
        'name': name,
        'container_name': container_name,
        'external_port': port,
        'internal_port': internal_port,
        'type': server_type,
        'version': version,
        'memory': memory,
    }

    # Create server data directory (inside container mount)
    data_path = os.path.join(MC_DATA_DIR, container_name)
    os.makedirs(data_path, exist_ok=True)

    # Create Docker container
    try:
        create_mc_container(server_config)
    except Exception as e:
        flash(f'Failed to create container: {e}', 'error')
        return redirect(url_for('dashboard'))

    # Add to config and save
    config.setdefault('servers', []).append(server_config)
    try:
        save_config(config)
    except Exception as e:
        flash(f'Server container created but failed to save config: {e}', 'error')
        return redirect(url_for('dashboard'))

    # Restart proxy to pick up new server
    if restart_proxy():
        flash(f'Created server "{name}" on port {port} and restarted proxy', 'success')
    else:
        flash(f'Created server "{name}" on port {port} but failed to restart proxy', 'warning')

    return redirect(url_for('dashboard'))


@app.route('/servers/<int:port>/remove', methods=['POST'])
@login_required
def remove_server(port):
    config = load_config()

    # Find server by port
    server_entry = None
    for srv in config.get('servers', []):
        if int(srv.get('external_port', 0)) == port:
            server_entry = srv
            break

    if not server_entry:
        flash(f'No server found on port {port}', 'error')
        return redirect(url_for('dashboard'))

    container_name = server_entry['container_name']
    server_name = server_entry.get('name', container_name)

    # Delete Docker container (data dir preserved)
    if not delete_mc_container(container_name):
        flash(f'Failed to delete container for "{server_name}"', 'error')
        return redirect(url_for('dashboard'))

    # Remove from config
    config['servers'] = [
        s for s in config.get('servers', [])
        if int(s.get('external_port', 0)) != port
    ]
    save_config(config)

    # Restart proxy
    if restart_proxy():
        flash(f'Removed server "{server_name}" and restarted proxy. Data preserved in mc_data/{container_name}/', 'success')
    else:
        flash(f'Removed server "{server_name}" but failed to restart proxy', 'warning')

    return redirect(url_for('dashboard'))


@app.route('/servers/<int:port>/start', methods=['POST'])
@login_required
def start_server(port):
    config = load_config()

    for srv in config.get('servers', []):
        if int(srv.get('external_port', 0)) == port:
            if start_mc_container(srv['container_name']):
                flash(f'Started server "{srv["name"]}"', 'success')
            else:
                flash(f'Failed to start server "{srv["name"]}"', 'error')
            return redirect(url_for('dashboard'))

    flash(f'No server found on port {port}', 'error')
    return redirect(url_for('dashboard'))


@app.route('/servers/<int:port>/stop', methods=['POST'])
@login_required
def stop_server(port):
    config = load_config()

    for srv in config.get('servers', []):
        if int(srv.get('external_port', 0)) == port:
            if stop_mc_container(srv['container_name']):
                flash(f'Stopped server "{srv["name"]}"', 'success')
            else:
                flash(f'Failed to stop server "{srv["name"]}"', 'error')
            return redirect(url_for('dashboard'))

    flash(f'No server found on port {port}', 'error')
    return redirect(url_for('dashboard'))


@app.route('/servers/<int:port>/edit')
@login_required
def edit_server(port):
    config = load_config()

    server_entry = None
    for srv in config.get('servers', []):
        if int(srv.get('external_port', 0)) == port:
            server_entry = srv
            break

    if not server_entry:
        flash(f'No server found on port {port}', 'error')
        return redirect(url_for('dashboard'))

    status = get_container_status(server_entry['container_name'])

    return render_template('edit_server.html',
                         server=server_entry,
                         status=status,
                         env_defaults=ENV_DEFAULTS)


@app.route('/servers/<int:port>/edit', methods=['POST'])
@login_required
def update_server(port):
    config = load_config()

    server_idx = None
    server_entry = None
    for i, srv in enumerate(config.get('servers', [])):
        if int(srv.get('external_port', 0)) == port:
            server_idx = i
            server_entry = srv
            break

    if server_entry is None:
        flash(f'No server found on port {port}', 'error')
        return redirect(url_for('dashboard'))

    # Parse basic fields
    new_name = request.form.get('name', '').strip()
    new_version = request.form.get('version', 'LATEST').strip()
    new_memory = request.form.get('memory', '2G')

    if not new_name:
        flash('Server name is required', 'error')
        return redirect(url_for('edit_server', port=port))

    if not new_version:
        new_version = 'LATEST'

    # Parse env vars — only store non-default values
    new_env = {}

    # String fields
    for key in ['MOTD', 'SEED', 'ICON', 'SPIGET_RESOURCES']:
        value = request.form.get(key, '').strip()
        if value and value != ENV_DEFAULTS.get(key, ''):
            new_env[key] = value

    # Select/enum fields
    for key in ['MODE', 'DIFFICULTY', 'LEVEL_TYPE']:
        value = request.form.get(key, '').strip()
        if value and value != ENV_DEFAULTS.get(key, ''):
            new_env[key] = value

    # Integer fields
    for key in INTEGER_ENV_VARS:
        value = request.form.get(key, '').strip()
        if value:
            try:
                int_value = int(value)
                if int_value != ENV_DEFAULTS.get(key):
                    new_env[key] = int_value
            except ValueError:
                pass

    # Boolean fields (checkbox: present = on = true, absent = false)
    for key in BOOLEAN_ENV_VARS:
        value = request.form.get(key) == 'on'
        if value != ENV_DEFAULTS.get(key, False):
            new_env[key] = value

    # Filter Paper-specific fields for non-Paper servers
    if server_entry.get('type') != 'PAPER':
        new_env.pop('SPIGET_RESOURCES', None)

    # Determine what changed
    old_env = server_entry.get('env', {})
    name_changed = new_name != server_entry.get('name', '')
    needs_recreation = (
        new_version != server_entry.get('version', 'LATEST') or
        new_memory != server_entry.get('memory', '2G') or
        new_env != old_env
    )

    # Update config entry
    server_entry['name'] = new_name
    server_entry['version'] = new_version
    server_entry['memory'] = new_memory
    if new_env:
        server_entry['env'] = new_env
    elif 'env' in server_entry:
        del server_entry['env']

    config['servers'][server_idx] = server_entry
    save_config(config)

    # Recreate container if needed
    if needs_recreation:
        success, was_running = recreate_mc_container(server_entry)
        if success:
            msg = f'Updated server "{new_name}" and recreated container.'
            if was_running:
                msg += ' Server was running and is now stopped — start it when ready.'
            flash(msg, 'success')
        else:
            flash(f'Updated config for "{new_name}" but failed to recreate container.', 'error')
    elif name_changed:
        flash(f'Updated server name to "{new_name}".', 'success')
    else:
        flash('No changes detected.', 'warning')

    # Restart proxy if name changed
    if name_changed:
        restart_proxy()

    return redirect(url_for('dashboard'))


@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    config = load_config()

    # Ensure notifications config exists with defaults
    if 'notifications' not in config:
        config['notifications'] = {
            'email': {
                'enabled': False, 'smtp_host': '', 'smtp_port': 587, 'smtp_tls': True,
                'smtp_user': '', 'smtp_password': '', 'from_address': '', 'to_addresses': [],
                'events': {'server_start': True, 'server_stop': True, 'player_join': False, 'player_leave': False, 'unauthorized_login': False}
            },
            'pushover': {
                'enabled': False, 'user_key': '', 'app_token': '', 'priority': 0,
                'events': {'server_start': True, 'server_stop': True, 'player_join': False, 'player_leave': False, 'unauthorized_login': False}
            }
        }

    if request.method == 'POST':
        config['timeout'] = int(request.form.get('timeout', 5))
        config['auto_shutdown'] = request.form.get('auto_shutdown') == 'on'

        save_config(config)

        if restart_proxy():
            flash('Settings saved and proxy restarted', 'success')
        else:
            flash('Settings saved but failed to restart proxy', 'warning')

        return redirect(url_for('settings'))

    return render_template('settings.html', config=config)


@app.route('/api/status')
@login_required
def api_status():
    """API endpoint for getting current server status (for AJAX refresh)"""
    config = load_config()

    # Read proxy state file
    proxy_state = {}
    try:
        with open('/config/proxy_state.json', 'r') as f:
            proxy_state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    client = get_docker_client()

    servers = []
    for srv in config.get('servers', []):
        container_name = srv.get('container_name', '')
        status = get_container_status(container_name)
        port_str = str(srv.get('external_port', ''))
        ps = proxy_state.get(port_str, {})
        env = srv.get('env', {})

        # Get container uptime if running
        started_at = None
        if status == 'running':
            try:
                container = client.containers.get(container_name)
                started_at = container.attrs['State'].get('StartedAt', '')
            except Exception:
                pass

        servers.append({
            'name': srv.get('name', ''),
            'external_port': srv.get('external_port'),
            'running': status == 'running',
            'status': status,
            'players': ps.get('players', 0),
            'shutdown_seconds': ps.get('shutdown_seconds'),
            'started_at': started_at,
            'motd': env.get('MOTD', 'A Minecraft Server'),
            'mode': env.get('MODE', 'survival'),
            'difficulty': env.get('DIFFICULTY', 'easy'),
            'max_players': env.get('MAX_PLAYERS', '20'),
        })

    servers.sort(key=lambda s: s['name'].lower())

    return jsonify(servers)


@app.route('/api/versions/<server_type>')
@login_required
def api_versions(server_type):
    """API endpoint returning available versions for a server type."""
    server_type = server_type.upper()
    if server_type not in VALID_SERVER_TYPES:
        return jsonify({'error': f'Invalid server type: {server_type}'}), 400
    versions = get_versions_for_type(server_type)
    return jsonify({'versions': ['LATEST'] + versions})


@app.route('/servers/<int:port>/console')
@login_required
def console(port):
    server, config = get_server_by_port(port)
    if not server:
        flash(f'No server found on port {port}', 'error')
        return redirect(url_for('dashboard'))
    status = get_container_status(server['container_name'])
    return render_template('console.html', server=server, status=status)


@app.route('/api/console/<int:port>')
@login_required
def api_console_logs(port):
    server, _ = get_server_by_port(port)
    if not server:
        return jsonify({'error': 'Server not found'}), 404

    container_name = server['container_name']
    lines = request.args.get('lines', 200, type=int)
    lines = max(1, min(lines, 1000))

    try:
        client = get_docker_client()
        container = client.containers.get(container_name)
        log_bytes = container.logs(tail=lines, timestamps=True)
        log_text = log_bytes.decode('utf-8', errors='replace')
        log_text = strip_ansi(log_text)
        log_lines = log_text.strip().split('\n') if log_text.strip() else []
        return jsonify({
            'lines': log_lines,
            'status': container.status,
        })
    except docker.errors.NotFound:
        return jsonify({'error': 'Container not found', 'status': 'not_found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/console/<int:port>/command', methods=['POST'])
@login_required
def api_console_command(port):
    server, _ = get_server_by_port(port)
    if not server:
        return jsonify({'success': False, 'error': 'Server not found'}), 404

    data = request.get_json(silent=True) or {}
    command = data.get('command', '').strip()
    if not command:
        command = request.form.get('command', '').strip()
    if not command:
        return jsonify({'success': False, 'error': 'No command provided'}), 400

    if send_mc_command(server['container_name'], command):
        return jsonify({'success': True, 'command': command})
    else:
        return jsonify({'success': False, 'error': 'Failed to send command. Server may not be running.'}), 400


@app.route('/servers/<int:port>/players')
@login_required
def players(port):
    server, config = get_server_by_port(port)
    if not server:
        flash(f'No server found on port {port}', 'error')
        return redirect(url_for('dashboard'))

    container_name = server['container_name']
    status = get_container_status(container_name)

    whitelist = read_player_json(container_name, 'whitelist.json')
    banned = read_player_json(container_name, 'banned-players.json')
    ops = read_player_json(container_name, 'ops.json')

    wl_enabled = read_server_property(container_name, 'white-list')
    whitelist_enabled = (wl_enabled == 'true') if wl_enabled else False

    return render_template('players.html',
                         server=server,
                         status=status,
                         whitelist=whitelist,
                         banned=banned,
                         ops=ops,
                         whitelist_enabled=whitelist_enabled)


@app.route('/servers/<int:port>/players/<list_name>/add', methods=['POST'])
@login_required
def add_player(port, list_name):
    if list_name not in PLAYER_LIST_CONFIG:
        flash('Invalid player list', 'error')
        return redirect(url_for('players', port=port))

    server, config = get_server_by_port(port)
    if not server:
        flash(f'No server found on port {port}', 'error')
        return redirect(url_for('dashboard'))

    container_name = server['container_name']
    status = get_container_status(container_name)
    username = request.form.get('username', '').strip()
    reason = request.form.get('reason', 'Banned by admin').strip()

    if not username:
        flash('Username is required', 'error')
        return redirect(url_for('players', port=port))

    list_cfg = PLAYER_LIST_CONFIG[list_name]

    if status == 'running':
        cmd = list_cfg['add_cmd'].format(name=username)
        if list_name == 'banned' and reason:
            cmd = f'ban {username} {reason}'
        if send_mc_command(container_name, cmd):
            time.sleep(1)  # Wait for server to process command and update files
            flash(f'Sent command: {cmd}', 'success')
        else:
            flash('Failed to send command to server', 'error')
    else:
        uuid, canonical_name = lookup_mojang_uuid(username)
        if not uuid:
            flash(f'Could not find player "{username}" via Mojang API. Check the spelling.', 'error')
            return redirect(url_for('players', port=port))

        player_list = read_player_json(container_name, list_cfg['filename'])

        if any(p.get('name', '').lower() == canonical_name.lower() for p in player_list):
            flash(f'"{canonical_name}" is already in the {list_name} list', 'warning')
            return redirect(url_for('players', port=port))

        if list_name == 'whitelist':
            entry = {'uuid': uuid, 'name': canonical_name}
        elif list_name == 'banned':
            from datetime import datetime
            entry = {
                'uuid': uuid,
                'name': canonical_name,
                'created': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S +0000'),
                'source': 'Server',
                'reason': reason or 'Banned by admin',
                'expires': 'forever',
            }
        elif list_name == 'ops':
            entry = {
                'uuid': uuid,
                'name': canonical_name,
                'level': 4,
                'bypassesPlayerLimit': False,
            }
        else:
            entry = {'uuid': uuid, 'name': canonical_name}

        player_list.append(entry)

        if write_player_json(container_name, list_cfg['filename'], player_list):
            flash(f'Added "{canonical_name}" to {list_name} list', 'success')
        else:
            flash(f'Failed to write {list_cfg["filename"]}', 'error')

    return redirect(url_for('players', port=port))


@app.route('/servers/<int:port>/players/<list_name>/remove', methods=['POST'])
@login_required
def remove_player(port, list_name):
    if list_name not in PLAYER_LIST_CONFIG:
        flash('Invalid player list', 'error')
        return redirect(url_for('players', port=port))

    server, config = get_server_by_port(port)
    if not server:
        flash(f'No server found on port {port}', 'error')
        return redirect(url_for('dashboard'))

    container_name = server['container_name']
    status = get_container_status(container_name)
    username = request.form.get('username', '').strip()

    if not username:
        flash('Username is required', 'error')
        return redirect(url_for('players', port=port))

    list_cfg = PLAYER_LIST_CONFIG[list_name]

    if status == 'running':
        cmd = list_cfg['remove_cmd'].format(name=username)
        if send_mc_command(container_name, cmd):
            time.sleep(1)  # Wait for server to process command and update files
            flash(f'Sent command: {cmd}', 'success')
        else:
            flash('Failed to send command to server', 'error')
    else:
        player_list = read_player_json(container_name, list_cfg['filename'])
        original_len = len(player_list)
        player_list = [p for p in player_list if p.get('name', '').lower() != username.lower()]

        if len(player_list) == original_len:
            flash(f'"{username}" not found in {list_name} list', 'warning')
            return redirect(url_for('players', port=port))

        if write_player_json(container_name, list_cfg['filename'], player_list):
            flash(f'Removed "{username}" from {list_name} list', 'success')
        else:
            flash(f'Failed to write {list_cfg["filename"]}', 'error')

    return redirect(url_for('players', port=port))


@app.route('/servers/<int:port>/players/whitelist/toggle', methods=['POST'])
@login_required
def toggle_whitelist(port):
    server, config = get_server_by_port(port)
    if not server:
        flash(f'No server found on port {port}', 'error')
        return redirect(url_for('dashboard'))

    container_name = server['container_name']
    status = get_container_status(container_name)

    current = read_server_property(container_name, 'white-list')
    new_value = 'false' if current == 'true' else 'true'

    if write_server_property(container_name, 'white-list', new_value):
        write_server_property(container_name, 'enforce-whitelist', new_value)

        if status == 'running':
            send_mc_command(container_name, 'whitelist on' if new_value == 'true' else 'whitelist off')
            flash(f'Whitelist {"enabled" if new_value == "true" else "disabled"} (applied immediately)', 'success')
        else:
            flash(f'Whitelist {"enabled" if new_value == "true" else "disabled"} (takes effect on next start)', 'success')
    else:
        flash('Failed to update server.properties', 'error')

    return redirect(url_for('players', port=port))


# --- Backup routes ---

@app.route('/servers/<int:port>/backups')
@login_required
def backups(port):
    server, config = get_server_by_port(port)
    if not server:
        flash(f'No server found on port {port}', 'error')
        return redirect(url_for('dashboard'))

    backup_name = get_backup_dir_name(server, config)
    status = get_container_status(server['container_name'])
    backup_list = backup_manager.list_backups(backup_name)
    in_progress = backup_manager.is_backup_in_progress(backup_name)
    settings = server.get('backup_settings', {
        'auto_enabled': False,
        'interval_hours': 6,
        'max_backups': 5,
    })

    return render_template('backups.html',
                         server=server,
                         status=status,
                         backups=backup_list,
                         in_progress=in_progress,
                         backup_settings=settings)


@app.route('/servers/<int:port>/backups/create', methods=['POST'])
@login_required
def create_backup_route(port):
    server, config = get_server_by_port(port)
    if not server:
        flash(f'No server found on port {port}', 'error')
        return redirect(url_for('dashboard'))

    backup_name = get_backup_dir_name(server, config)
    container_name = server['container_name']

    if backup_manager.is_backup_in_progress(backup_name):
        flash('A backup is already in progress', 'warning')
        return redirect(url_for('backups', port=port))

    def do_backup():
        success, msg, _ = backup_manager.create_backup(
            backup_name,
            container_name,
            backup_type='manual',
            send_mc_command_fn=send_mc_command,
            get_status_fn=get_container_status,
        )
        if success:
            print(f"[Backup] Manual backup completed for {backup_name}: {msg}")
        else:
            print(f"[Backup] Manual backup failed for {backup_name}: {msg}")

    thread = threading.Thread(target=do_backup, daemon=True)
    thread.start()

    flash('Backup started. The page will refresh when complete.', 'success')
    return redirect(url_for('backups', port=port))


@app.route('/servers/<int:port>/backups/<filename>/restore', methods=['POST'])
@login_required
def restore_backup_route(port, filename):
    server, config = get_server_by_port(port)
    if not server:
        flash(f'No server found on port {port}', 'error')
        return redirect(url_for('dashboard'))

    backup_name = get_backup_dir_name(server, config)
    success, msg = backup_manager.restore_backup(
        backup_name, server['container_name'], filename,
        stop_fn=stop_mc_container,
        start_fn=start_mc_container,
        get_status_fn=get_container_status,
    )

    flash(msg, 'success' if success else 'error')
    return redirect(url_for('backups', port=port))


@app.route('/servers/<int:port>/backups/<filename>/download')
@login_required
def download_backup(port, filename):
    server, config = get_server_by_port(port)
    if not server:
        flash(f'No server found on port {port}', 'error')
        return redirect(url_for('dashboard'))

    backup_name = get_backup_dir_name(server, config)
    filepath = backup_manager.get_backup_filepath(backup_name, filename)
    if not filepath:
        flash('Backup file not found', 'error')
        return redirect(url_for('backups', port=port))

    return send_file(filepath, as_attachment=True, download_name=filename)


@app.route('/servers/<int:port>/backups/<filename>/delete', methods=['POST'])
@login_required
def delete_backup_route(port, filename):
    server, config = get_server_by_port(port)
    if not server:
        flash(f'No server found on port {port}', 'error')
        return redirect(url_for('dashboard'))

    backup_name = get_backup_dir_name(server, config)
    success, msg = backup_manager.delete_backup(backup_name, filename)
    flash(msg, 'success' if success else 'error')
    return redirect(url_for('backups', port=port))


@app.route('/servers/<int:port>/backups/settings', methods=['POST'])
@login_required
def backup_settings(port):
    server, config = get_server_by_port(port)
    if not server:
        flash(f'No server found on port {port}', 'error')
        return redirect(url_for('dashboard'))

    auto_enabled = request.form.get('auto_enabled') == 'on'
    interval_hours = int(request.form.get('interval_hours', 6))
    max_backups = int(request.form.get('max_backups', 5))

    if interval_hours not in (1, 2, 4, 6, 12, 24):
        interval_hours = 6
    max_backups = max(3, min(20, max_backups))

    for i, srv in enumerate(config.get('servers', [])):
        if int(srv.get('external_port', 0)) == port:
            config['servers'][i]['backup_settings'] = {
                'auto_enabled': auto_enabled,
                'interval_hours': interval_hours,
                'max_backups': max_backups,
            }
            break
    save_config(config)

    backup_name = get_backup_dir_name(server, config)

    if auto_enabled:
        backup_manager.schedule_auto_backup(
            backup_name, server['container_name'], interval_hours, max_backups,
            send_mc_command, get_container_status,
        )
        flash(f'Auto-backup enabled: every {interval_hours}h, keep {max_backups}', 'success')
    else:
        backup_manager.cancel_auto_backup(backup_name)
        flash('Auto-backup disabled', 'success')

    return redirect(url_for('backups', port=port))


@app.route('/api/backups/<int:port>/status')
@login_required
def api_backup_status(port):
    server, config = get_server_by_port(port)
    if not server:
        return jsonify({'error': 'Server not found'}), 404

    backup_name = get_backup_dir_name(server, config)
    return jsonify({
        'in_progress': backup_manager.is_backup_in_progress(backup_name),
        'backups': backup_manager.list_backups(backup_name),
    })


# --- Mod/Plugin routes ---

# Server types that support mods/plugins
MODDED_SERVER_TYPES = {'PAPER', 'SPIGOT', 'FABRIC', 'FORGE'}


@app.route('/servers/<int:port>/mods')
@login_required
def server_mods(port):
    """Mod/plugin management page."""
    server, config = get_server_by_port(port)
    if not server:
        flash('Server not found', 'error')
        return redirect(url_for('dashboard'))

    server_type = server.get('type', 'VANILLA')
    status = get_container_status(server['container_name'])

    # Determine directory name based on server type
    mod_dir = 'plugins' if server_type in ('PAPER', 'SPIGOT') else 'mods'
    mod_path = os.path.join(MC_DATA_DIR, server['container_name'], mod_dir)

    # List installed mods/plugins (.jar files)
    installed = []
    if os.path.isdir(mod_path):
        for f in sorted(os.listdir(mod_path)):
            if f.endswith('.jar'):
                full = os.path.join(mod_path, f)
                stat = os.stat(full)
                size_kb = stat.st_size / 1024
                if size_kb >= 1024:
                    size_human = f'{size_kb / 1024:.1f} MB'
                else:
                    size_human = f'{size_kb:.1f} KB'
                installed.append({
                    'name': f,
                    'size': stat.st_size,
                    'size_human': size_human,
                    'modified': time.strftime('%Y-%m-%d %H:%M', time.localtime(stat.st_mtime)),
                })

    # Get env-based mod lists from config
    env = server.get('env', {})

    return render_template('mods.html',
        server=server,
        status=status,
        server_type=server_type,
        mod_dir=mod_dir,
        installed=installed,
        spiget_resources=env.get('SPIGET_RESOURCES', ''),
        modrinth_projects=env.get('MODRINTH_PROJECTS', ''),
        is_modded=server_type in MODDED_SERVER_TYPES,
    )


@app.route('/servers/<int:port>/mods/upload', methods=['POST'])
@login_required
def upload_mod(port):
    """Upload a .jar mod/plugin file."""
    server, config = get_server_by_port(port)
    if not server:
        flash('Server not found', 'error')
        return redirect(url_for('dashboard'))

    file = request.files.get('modfile')
    if not file or not file.filename:
        flash('No file selected', 'error')
        return redirect(url_for('server_mods', port=port))

    if not file.filename.endswith('.jar'):
        flash('Only .jar files are allowed', 'error')
        return redirect(url_for('server_mods', port=port))

    server_type = server.get('type', 'VANILLA')
    mod_dir = 'plugins' if server_type in ('PAPER', 'SPIGOT') else 'mods'
    mod_path = os.path.join(MC_DATA_DIR, server['container_name'], mod_dir)

    os.makedirs(mod_path, exist_ok=True)
    filename = secure_filename(file.filename)
    if not filename.endswith('.jar'):
        filename += '.jar'

    filepath = os.path.join(mod_path, filename)
    file.save(filepath)

    flash(f'Uploaded {filename}. Restart the server to load it.', 'success')
    return redirect(url_for('server_mods', port=port))


@app.route('/servers/<int:port>/mods/delete', methods=['POST'])
@login_required
def delete_mod(port):
    """Remove an installed mod/plugin .jar and its auto-download source."""
    server, config = get_server_by_port(port)
    if not server:
        flash('Server not found', 'error')
        return redirect(url_for('dashboard'))

    filename = request.form.get('filename', '')
    if not filename or '..' in filename or '/' in filename or '\\' in filename:
        flash('Invalid filename', 'error')
        return redirect(url_for('server_mods', port=port))

    server_type = server.get('type', 'VANILLA')
    mod_dir = 'plugins' if server_type in ('PAPER', 'SPIGOT') else 'mods'
    mod_path = os.path.join(MC_DATA_DIR, server['container_name'], mod_dir)
    filepath = os.path.join(mod_path, filename)

    if os.path.isfile(filepath):
        # Check if this mod was from an auto-download source
        source_info = scheduler.get_mod_source(mod_path, filename)
        if source_info:
            # Remove from auto-download config
            source_type = source_info.get('type')
            source_id = source_info.get('id')

            # Find server index in config for updating
            config = load_config()
            for i, srv in enumerate(config.get('servers', [])):
                if int(srv.get('external_port', 0)) == port:
                    env = srv.setdefault('env', {})

                    if source_type == 'modrinth':
                        current = env.get('MODRINTH_PROJECTS', '')
                        projects = [p.strip() for p in current.split(',') if p.strip()]
                        if source_id in projects:
                            projects.remove(source_id)
                            env['MODRINTH_PROJECTS'] = ','.join(projects)

                    elif source_type == 'spiget':
                        current = env.get('SPIGET_RESOURCES', '')
                        resources = [r.strip() for r in current.split(',') if r.strip()]
                        if source_id in resources:
                            resources.remove(source_id)
                            env['SPIGET_RESOURCES'] = ','.join(resources)

                    save_config(config)
                    break

            # Remove from tracking file
            scheduler.remove_mod_source(mod_path, filename)

        os.remove(filepath)
        flash(f'Removed {filename}. Restart the server to apply.', 'success')
    else:
        flash('File not found', 'error')

    return redirect(url_for('server_mods', port=port))


@app.route('/servers/<int:port>/mods/config', methods=['POST'])
@login_required
def update_mod_config(port):
    """Update env-based mod/plugin sources (Modrinth, Spiget, etc.)."""
    config = load_config()

    server_idx = None
    server = None
    for i, srv in enumerate(config.get('servers', [])):
        if int(srv.get('external_port', 0)) == port:
            server_idx = i
            server = srv
            break

    if server is None:
        flash('Server not found', 'error')
        return redirect(url_for('dashboard'))

    env = server.get('env', {})
    server_type = server.get('type', 'VANILLA')

    spiget = request.form.get('spiget_resources', '').strip()
    modrinth = request.form.get('modrinth_projects', '').strip()

    # Only allow SPIGET_RESOURCES for Paper/Spigot
    if server_type in ('PAPER', 'SPIGOT'):
        if spiget:
            env['SPIGET_RESOURCES'] = spiget
        else:
            env.pop('SPIGET_RESOURCES', None)

    # MODRINTH_PROJECTS works for all modded types
    if modrinth:
        env['MODRINTH_PROJECTS'] = modrinth
    else:
        env.pop('MODRINTH_PROJECTS', None)

    if env:
        server['env'] = env
    elif 'env' in server:
        del server['env']

    config['servers'][server_idx] = server
    save_config(config)

    # Download mods immediately so they're ready for next server start
    downloaded, errors, results = scheduler.download_server_mods(server, MC_DATA_DIR)

    if downloaded > 0 or errors > 0:
        status = get_container_status(server['container_name'])
        msg = f'Downloaded {downloaded} mod(s)'
        if errors > 0:
            msg += f', {errors} error(s)'
        if status == 'running':
            msg += '. Restart the server to load new mods.'
        else:
            msg += '. Mods ready for next server start.'
        flash(msg, 'success' if errors == 0 else 'warning')
    elif modrinth or spiget:
        flash('Config saved but no mods were downloaded. Check project slugs/IDs.', 'warning')
    else:
        flash('Mod sources cleared.', 'success')

    return redirect(url_for('server_mods', port=port))


@app.route('/api/mods/search')
@login_required
def search_mods():
    """Proxy Modrinth API search to avoid CORS issues."""
    query = request.args.get('q', '')
    server_type = request.args.get('type', '').upper()
    offset = request.args.get('offset', '0')

    # Map server types to Modrinth loaders
    loader_map = {
        'PAPER': 'paper',
        'SPIGOT': 'spigot',
        'FABRIC': 'fabric',
        'FORGE': 'forge',
    }

    loader = loader_map.get(server_type, '')

    params = {
        'query': query,
        'limit': 20,
        'offset': offset,
        'index': 'relevance',
    }

    # Build facets for filtering
    if loader:
        if server_type in ('PAPER', 'SPIGOT'):
            # Plugins
            params['facets'] = f'[["project_type:plugin"],["categories:{loader}"]]'
        else:
            # Mods
            params['facets'] = f'[["project_type:mod"],["categories:{loader}"]]'

    try:
        resp = requests.get(
            'https://api.modrinth.com/v2/search',
            params=params,
            headers={'User-Agent': 'MCServerManager/1.0'},
            timeout=10
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.RequestException as e:
        return jsonify({'error': f'Modrinth API error: {e}'}), 500
    except Exception as e:
        return jsonify({'error': f'Search error: {e}'}), 500


@app.route('/api/spiget/search')
@login_required
def search_spiget():
    """Proxy Spiget API search for SpigotMC plugins."""
    query = request.args.get('q', '')
    page = request.args.get('page', '1')
    size = request.args.get('size', '20')

    if not query:
        return jsonify({'error': 'No search query provided'}), 400

    try:
        resp = requests.get(
            f'https://api.spiget.org/v2/search/resources/{query}',
            params={
                'field': 'name',
                'size': size,
                'page': page,
                'sort': '-downloads',
            },
            headers={'User-Agent': 'MCServerManager/1.0'},
            timeout=10
        )
        resp.raise_for_status()
        resources = resp.json()

        # Transform to a consistent format
        results = []
        for r in resources:
            results.append({
                'id': r.get('id'),
                'name': r.get('name', 'Unknown'),
                'tag': r.get('tag', ''),
                'downloads': r.get('downloads', 0),
                'rating': r.get('rating', {}).get('average', 0),
                'icon_url': f"https://api.spiget.org/v2/resources/{r.get('id')}/icon" if r.get('icon', {}).get('data') else None,
            })

        return jsonify({'results': results})
    except requests.RequestException as e:
        return jsonify({'error': str(e)}), 500


@app.route('/logs')
@login_required
def logs():
    """View usage logs"""
    # Get list of log files
    log_files = []
    if os.path.isdir(LOGS_DIR):
        files = glob_module.glob(os.path.join(LOGS_DIR, 'usage-*.log'))
        # Sort by date descending (newest first)
        log_files = sorted([os.path.basename(f) for f in files], reverse=True)

    # Get selected file (default to latest)
    selected_file = request.args.get('file')
    if not selected_file and log_files:
        selected_file = log_files[0]

    # Read log entries
    entries = []
    if selected_file and selected_file in log_files:
        log_path = os.path.join(LOGS_DIR, selected_file)
        try:
            with open(log_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except Exception as e:
            flash(f'Error reading log file: {e}', 'error')

    # Reverse to show newest first
    entries.reverse()

    # Map generic server names to configured names using port
    config = load_config()
    port_names = {int(s['external_port']): s['name'] for s in config.get('servers', []) if 'external_port' in s and 'name' in s}
    for entry in entries:
        port = entry.get('port')
        if port and port in port_names:
            entry['server_name'] = port_names[port]

    return render_template('logs.html',
                         log_files=log_files,
                         selected_file=selected_file,
                         entries=entries)


@app.route('/api/logs')
@login_required
def api_logs():
    """API endpoint for getting log entries (for AJAX refresh)"""
    selected_file = request.args.get('file')

    # Get list of log files
    log_files = []
    if os.path.isdir(LOGS_DIR):
        files = glob_module.glob(os.path.join(LOGS_DIR, 'usage-*.log'))
        log_files = sorted([os.path.basename(f) for f in files], reverse=True)

    # Default to latest if not specified
    if not selected_file and log_files:
        selected_file = log_files[0]

    # Read log entries
    entries = []
    if selected_file and selected_file in log_files:
        log_path = os.path.join(LOGS_DIR, selected_file)
        try:
            with open(log_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass

    # Reverse to show newest first
    entries.reverse()

    # Map generic server names to configured names using port
    config = load_config()
    port_names = {int(s['external_port']): s['name'] for s in config.get('servers', []) if 'external_port' in s and 'name' in s}
    for entry in entries:
        port = entry.get('port')
        if port and port in port_names:
            entry['server_name'] = port_names[port]

    return jsonify({'entries': entries, 'count': len(entries)})


@app.route('/logs/download/<filename>')
@login_required
def download_log(filename):
    """Download a log file"""
    # Validate filename to prevent directory traversal
    if not filename.startswith('usage-') or not filename.endswith('.log'):
        flash('Invalid log file', 'error')
        return redirect(url_for('logs'))

    log_path = os.path.join(LOGS_DIR, filename)
    if not os.path.isfile(log_path):
        flash('Log file not found', 'error')
        return redirect(url_for('logs'))

    return send_file(log_path, as_attachment=True, download_name=filename)


@app.route('/notifications', methods=['GET', 'POST'])
@login_required
def notifications():
    """Notification settings page"""
    config = load_config()

    # Ensure notifications config exists with defaults
    if 'notifications' not in config:
        config['notifications'] = {
            'email': {
                'enabled': False,
                'smtp_host': '',
                'smtp_port': 587,
                'smtp_tls': True,
                'smtp_user': '',
                'smtp_password': '',
                'from_address': '',
                'to_addresses': [],
                'events': {
                    'server_start': True,
                    'server_stop': True,
                    'player_join': False,
                    'player_leave': False,
                    'unauthorized_login': False
                }
            },
            'pushover': {
                'enabled': False,
                'user_key': '',
                'app_token': '',
                'priority': 0,
                'events': {
                    'server_start': True,
                    'server_stop': True,
                    'player_join': False,
                    'player_leave': False,
                    'unauthorized_login': False
                }
            }
        }

    if request.method == 'POST':
        # Email settings
        config['notifications']['email']['enabled'] = request.form.get('email_enabled') == 'on'
        config['notifications']['email']['smtp_host'] = request.form.get('smtp_host', '')
        config['notifications']['email']['smtp_port'] = int(request.form.get('smtp_port', 587))
        config['notifications']['email']['smtp_tls'] = request.form.get('smtp_tls') == 'on'
        config['notifications']['email']['smtp_user'] = request.form.get('smtp_user', '')

        # Only update password if provided
        new_smtp_password = request.form.get('smtp_password', '')
        if new_smtp_password:
            config['notifications']['email']['smtp_password'] = new_smtp_password

        config['notifications']['email']['from_address'] = request.form.get('from_address', '')

        # Parse to_addresses (comma-separated)
        to_addresses = request.form.get('to_addresses', '')
        config['notifications']['email']['to_addresses'] = [
            addr.strip() for addr in to_addresses.split(',') if addr.strip()
        ]

        # Email events
        config['notifications']['email']['events'] = {
            'server_start': request.form.get('email_server_start') == 'on',
            'server_stop': request.form.get('email_server_stop') == 'on',
            'player_join': request.form.get('email_player_join') == 'on',
            'player_leave': request.form.get('email_player_leave') == 'on',
            'unauthorized_login': request.form.get('email_unauthorized_login') == 'on'
        }

        # Pushover settings
        config['notifications']['pushover']['enabled'] = request.form.get('pushover_enabled') == 'on'
        config['notifications']['pushover']['user_key'] = request.form.get('pushover_user_key', '')

        # Only update app token if provided
        new_app_token = request.form.get('pushover_app_token', '')
        if new_app_token:
            config['notifications']['pushover']['app_token'] = new_app_token

        config['notifications']['pushover']['priority'] = int(request.form.get('pushover_priority', 0))

        # Pushover events
        config['notifications']['pushover']['events'] = {
            'server_start': request.form.get('pushover_server_start') == 'on',
            'server_stop': request.form.get('pushover_server_stop') == 'on',
            'player_join': request.form.get('pushover_player_join') == 'on',
            'player_leave': request.form.get('pushover_player_leave') == 'on',
            'unauthorized_login': request.form.get('pushover_unauthorized_login') == 'on'
        }

        save_config(config)

        if restart_proxy():
            flash('Notification settings saved and proxy restarted', 'success')
        else:
            flash('Notification settings saved but failed to restart proxy', 'warning')

        return redirect(url_for('settings'))

    return render_template('notifications.html', config=config)


@app.route('/notifications/test/<service>', methods=['POST'])
@login_required
def test_notification(service):
    """Test notification service"""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    import requests

    config = load_config()

    if service == 'email':
        email_config = config.get('notifications', {}).get('email', {})
        host = email_config.get('smtp_host', '')
        port = email_config.get('smtp_port', 587)
        tls = email_config.get('smtp_tls', True)
        user = email_config.get('smtp_user', '')
        password = email_config.get('smtp_password', '')
        from_address = email_config.get('from_address', '')
        to_addresses = email_config.get('to_addresses', [])

        if not host:
            return jsonify({'success': False, 'message': 'SMTP host not configured'})
        if not to_addresses:
            return jsonify({'success': False, 'message': 'No recipient addresses configured'})
        if not from_address:
            return jsonify({'success': False, 'message': 'From address not configured'})

        try:
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
            msg['Subject'] = '[MC] Test Notification'
            msg.attach(MIMEText('This is a test notification from MC Server Manager.', 'plain'))

            server.sendmail(from_address, to_addresses, msg.as_string())
            server.quit()
            return jsonify({'success': True, 'message': 'Test email sent successfully'})
        except smtplib.SMTPAuthenticationError:
            return jsonify({'success': False, 'message': 'SMTP authentication failed'})
        except smtplib.SMTPConnectError:
            return jsonify({'success': False, 'message': f'Could not connect to SMTP server {host}:{port}'})
        except Exception as e:
            return jsonify({'success': False, 'message': f'Error: {str(e)}'})

    elif service == 'pushover':
        pushover_config = config.get('notifications', {}).get('pushover', {})
        user_key = pushover_config.get('user_key', '')
        app_token = pushover_config.get('app_token', '')
        priority = pushover_config.get('priority', 0)

        if not user_key:
            return jsonify({'success': False, 'message': 'User key not configured'})
        if not app_token:
            return jsonify({'success': False, 'message': 'App token not configured'})

        try:
            data = {
                'token': app_token,
                'user': user_key,
                'title': '[MC] Test Notification',
                'message': 'This is a test notification from MC Server Manager.',
                'priority': priority
            }
            resp = requests.post('https://api.pushover.net/1/messages.json', data=data, timeout=10)

            if resp.status_code == 200:
                return jsonify({'success': True, 'message': 'Test notification sent successfully'})
            else:
                error = resp.json().get('errors', ['Unknown error'])
                return jsonify({'success': False, 'message': f"Pushover error: {', '.join(error)}"})
        except Exception as e:
            return jsonify({'success': False, 'message': f'Error: {str(e)}'})

    else:
        return jsonify({'success': False, 'message': 'Unknown service'})


# --- Scheduled Tasks routes ---

@app.route('/tasks')
@login_required
def tasks():
    config = load_config()
    all_tasks = config.get('scheduled_tasks', [])
    servers = config.get('servers', [])

    # Build a port -> name lookup
    server_names = {}
    for srv in servers:
        server_names[int(srv.get('external_port', 0))] = srv.get('name', 'Unknown')

    # Enrich tasks for the template
    enriched = []
    for t in all_tasks:
        entry = dict(t)
        entry['server_name'] = server_names.get(t.get('server_port', 0), 'Unknown')
        entry['type_label'] = scheduler.TASK_TYPES.get(t['type'], t['type'])
        if t['schedule_type'] == 'preset':
            preset = scheduler.SCHEDULE_PRESETS.get(t['schedule_value'], {})
            entry['preset_label'] = preset.get('label', t['schedule_value'])
        enriched.append(entry)

    return render_template('tasks.html',
                          tasks=enriched,
                          servers=servers,
                          task_types=scheduler.TASK_TYPES,
                          presets=scheduler.SCHEDULE_PRESETS)


@app.route('/tasks/create', methods=['POST'])
@login_required
def create_task():
    task_type = request.form.get('type', '')
    server_port = request.form.get('server_port', type=int)
    schedule_type = request.form.get('schedule_type', 'preset')
    schedule_value = request.form.get('schedule_value', '').strip()
    enabled = request.form.get('enabled') == 'on'

    if task_type not in scheduler.TASK_TYPES:
        flash('Invalid task type', 'error')
        return redirect(url_for('tasks'))

    # Validate server exists
    config = load_config()
    if not _find_server_by_port(config, server_port):
        flash('Server not found', 'error')
        return redirect(url_for('tasks'))

    # Validate schedule
    if schedule_type == 'cron':
        if not schedule_value or not scheduler.validate_cron(schedule_value):
            flash('Invalid cron expression. Use 5-field format: minute hour day month weekday', 'error')
            return redirect(url_for('tasks'))
    elif schedule_type == 'preset':
        if schedule_value not in scheduler.SCHEDULE_PRESETS:
            flash('Invalid schedule preset', 'error')
            return redirect(url_for('tasks'))
    else:
        flash('Invalid schedule type', 'error')
        return redirect(url_for('tasks'))

    # Build type-specific config
    task_config = {}
    if task_type == 'version_check':
        task_config['action'] = request.form.get('vc_action', 'notify')
        if task_config['action'] not in ('notify', 'auto_update'):
            task_config['action'] = 'notify'
        task_config['auto_restart'] = request.form.get('vc_auto_restart') == 'on'
    elif task_type == 'command':
        task_config['command'] = request.form.get('mc_command', '').strip()
        if not task_config['command']:
            flash('Command is required', 'error')
            return redirect(url_for('tasks'))
    elif task_type == 'broadcast':
        task_config['message'] = request.form.get('bc_message', '').strip()
        if not task_config['message']:
            flash('Message is required', 'error')
            return redirect(url_for('tasks'))

    task = {
        'enabled': enabled,
        'server_port': server_port,
        'type': task_type,
        'schedule_type': schedule_type,
        'schedule_value': schedule_value,
        'config': task_config,
    }

    scheduler.add_task(task)
    flash(f'Scheduled task created: {scheduler.TASK_TYPES[task_type]}', 'success')
    return redirect(url_for('tasks'))


@app.route('/tasks/<task_id>/toggle', methods=['POST'])
@login_required
def toggle_task(task_id):
    config = load_config()
    current_task = None
    for t in config.get('scheduled_tasks', []):
        if t['id'] == task_id:
            current_task = t
            break

    if not current_task:
        flash('Task not found', 'error')
        return redirect(url_for('tasks'))

    new_enabled = not current_task.get('enabled', False)
    scheduler.toggle_task(task_id, new_enabled)
    flash(f'Task {"enabled" if new_enabled else "disabled"}', 'success')
    return redirect(url_for('tasks'))


@app.route('/tasks/<task_id>/delete', methods=['POST'])
@login_required
def delete_task(task_id):
    scheduler.remove_task(task_id)
    flash('Scheduled task deleted', 'success')
    return redirect(url_for('tasks'))


@app.route('/tasks/<task_id>/run', methods=['POST'])
@login_required
def run_task(task_id):
    config = load_config()
    found = any(t['id'] == task_id for t in config.get('scheduled_tasks', []))
    if not found:
        flash('Task not found', 'error')
        return redirect(url_for('tasks'))

    scheduler.run_task_now(task_id)
    flash('Task triggered — check back for results', 'success')
    return redirect(url_for('tasks'))


def _find_server_by_port(config, port):
    """Look up a server config entry by external port (for task routes)."""
    for srv in config.get('servers', []):
        if int(srv.get('external_port', 0)) == port:
            return srv
    return None


# Initialize auto-backup scheduler on startup
_startup_config = load_config()
backup_manager.init_auto_backups(_startup_config, get_backup_dir_name, send_mc_command, get_container_status)

scheduler.init_scheduler(
    load_config_fn=load_config,
    save_config_fn=save_config,
    get_container_status_fn=get_container_status,
    send_mc_command_fn=send_mc_command,
    stop_mc_container_fn=stop_mc_container,
    start_mc_container_fn=start_mc_container,
    recreate_mc_container_fn=recreate_mc_container,
    get_versions_for_type_fn=get_versions_for_type,
    mc_data_dir=MC_DATA_DIR,
)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)
