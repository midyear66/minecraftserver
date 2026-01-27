#!/usr/bin/env python3
"""
Migration script: Import servers from Crafty Controller to MC Server Manager.

Scans ~/crafty/docker/servers/ for existing Minecraft servers, copies essential
data to ~/minecraftserver/mc_data/, creates Docker containers using
itzg/minecraft-server, and generates proxy/config.json.

Run on the host (not in a container):
    cd ~/minecraftserver
    python3 migrate.py
"""

import os
import sys
import json
import re
import shutil
import subprocess
from pathlib import Path

# Paths
CRAFTY_SERVERS_DIR = os.path.expanduser('~/crafty/docker/servers')
CRAFTY_CONFIG_PATH = os.path.expanduser('~/crafty/proxy/config.json')
MC_DATA_DIR = os.path.expanduser('~/minecraftserver/mc_data')
OUTPUT_CONFIG_PATH = os.path.expanduser('~/minecraftserver/proxy/config.json')

# Files/dirs to copy from each server
COPY_DIRS = [
    'world', 'world_nether', 'world_the_end',
    'plugins',
]

COPY_FILES = [
    'server.properties',
    'ops.json',
    'whitelist.json',
    'banned-players.json',
    'banned-ips.json',
    'icon.png',
    'bukkit.yml',
    'spigot.yml',
    'paper.yml',
    'paper-global.yml',
    'paper-world-defaults.yml',
]

# Files/dirs to skip
SKIP_PATTERNS = [
    'libraries', 'versions', 'logs', 'crash-reports',
    'db_stats', 'old', 'cache',
    '*.jar', 'crafty_managed.txt',
    'server.config', 'cron.config',
    '.paper-remapped',
]

# Internal port allocation
INTERNAL_PORT_START = 30001


def discover_servers():
    """Scan Crafty servers directory and discover Minecraft servers."""
    servers = []

    if not os.path.isdir(CRAFTY_SERVERS_DIR):
        print(f"Error: Crafty servers directory not found: {CRAFTY_SERVERS_DIR}")
        sys.exit(1)

    for uuid_dir in sorted(os.listdir(CRAFTY_SERVERS_DIR)):
        server_path = os.path.join(CRAFTY_SERVERS_DIR, uuid_dir)
        if not os.path.isdir(server_path):
            continue

        props_path = os.path.join(server_path, 'server.properties')
        if not os.path.isfile(props_path):
            continue

        # Parse server.properties
        props = {}
        with open(props_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    props[key.strip()] = value.strip()

        port = int(props.get('server-port', 25565))

        # Detect server type from JAR files
        server_type = 'VANILLA'
        version = 'LATEST'
        jar_files = [f for f in os.listdir(server_path) if f.endswith('.jar')]

        for jar in jar_files:
            jar_lower = jar.lower()
            if 'paper' in jar_lower:
                server_type = 'PAPER'
                # Extract version: paper-1.21.11.jar -> 1.21.11
                match = re.search(r'paper[- ]?(\d+\.\d+(?:\.\d+)?)', jar_lower)
                if match:
                    version = match.group(1)
                break
            elif 'spigot' in jar_lower:
                server_type = 'SPIGOT'
                match = re.search(r'spigot[- ]?(\d+\.\d+(?:\.\d+)?)', jar_lower)
                if match:
                    version = match.group(1)
                break
            elif 'fabric' in jar_lower:
                server_type = 'FABRIC'
                break
            elif 'forge' in jar_lower:
                server_type = 'FORGE'
                break

        # If still VANILLA, try to get version from JAR name
        if server_type == 'VANILLA':
            for jar in jar_files:
                match = re.search(r'vanilla[- ]?(\d+\.\d+(?:\.\d+)?)', jar.lower())
                if match:
                    version = match.group(1)
                    break

        # Detect memory from server.config if present
        memory = '2G'
        config_path = os.path.join(server_path, 'server.config')
        if os.path.isfile(config_path):
            with open(config_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('java_xmx='):
                        mb = int(line.split('=', 1)[1])
                        if mb >= 1024:
                            memory = f'{mb // 1024}G'
                        else:
                            memory = f'{mb}M'
                        break

        # Check notable features
        has_plugins = os.path.isdir(os.path.join(server_path, 'plugins'))
        has_icon = os.path.isfile(os.path.join(server_path, 'icon.png'))
        motd = props.get('motd', '')

        servers.append({
            'uuid': uuid_dir,
            'path': server_path,
            'port': port,
            'type': server_type,
            'version': version,
            'memory': memory,
            'has_plugins': has_plugins,
            'has_icon': has_icon,
            'motd': motd,
        })

    return servers


def prompt_server_names(servers):
    """Interactively prompt for server names."""
    print("\n=== Discovered Servers ===\n")
    for i, srv in enumerate(servers):
        features = []
        if srv['has_plugins']:
            features.append('plugins')
        if srv['has_icon']:
            features.append('icon.png')
        if srv['motd']:
            features.append(f'motd: {srv["motd"][:50]}')

        features_str = f' [{", ".join(features)}]' if features else ''
        print(f"  {i+1}. Port {srv['port']} | {srv['type']} {srv['version']} | {srv['memory']} RAM{features_str}")

    print()

    for srv in servers:
        while True:
            default = f"Server_{srv['port']}"
            name = input(f"  Name for port {srv['port']} ({srv['type']} {srv['version']}) [{default}]: ").strip()
            if not name:
                name = default
            # Validate
            if name:
                srv['name'] = name
                break

    return servers


def sanitize_container_name(name):
    """Convert a server name to a valid Docker container name."""
    sanitized = re.sub(r'[^a-z0-9_.-]', '_', name.lower().strip())
    sanitized = re.sub(r'_+', '_', sanitized).strip('_')
    if not sanitized:
        sanitized = 'mc_server'
    if not sanitized.startswith('mc_'):
        sanitized = 'mc_' + sanitized
    return sanitized


def copy_server_data(srv, dest_path):
    """Copy essential server data from Crafty to mc_data."""
    src_path = srv['path']

    os.makedirs(dest_path, exist_ok=True)

    # Copy directories
    for dirname in COPY_DIRS:
        src = os.path.join(src_path, dirname)
        dst = os.path.join(dest_path, dirname)
        if os.path.isdir(src):
            print(f"    Copying {dirname}/...")
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst, symlinks=True,
                           ignore=shutil.ignore_patterns('.paper-remapped'))

    # Copy files
    for filename in COPY_FILES:
        src = os.path.join(src_path, filename)
        dst = os.path.join(dest_path, filename)
        if os.path.isfile(src):
            print(f"    Copying {filename}")
            shutil.copy2(src, dst)

    # Fix server-port to 25565 (Docker maps internal_port -> container:25565)
    props_path = os.path.join(dest_path, 'server.properties')
    if os.path.isfile(props_path):
        with open(props_path, 'r') as f:
            lines = f.readlines()
        with open(props_path, 'w') as f:
            for line in lines:
                if line.strip().startswith('server-port='):
                    f.write('server-port=25565\n')
                else:
                    f.write(line)
        print("    Fixed server-port=25565")

    # Count what was copied
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(dest_path):
        for f in filenames:
            total_size += os.path.getsize(os.path.join(dirpath, f))

    print(f"    Total: {total_size / (1024*1024):.1f} MB")


def create_docker_container(srv, container_name, internal_port, data_path):
    """Create a Docker container for a Minecraft server (stopped)."""
    env_vars = [
        'EULA=TRUE',
        f'TYPE={srv["type"]}',
        f'VERSION={srv["version"]}',
        f'MEMORY={srv["memory"]}',
        'ENABLE_QUERY=false',
        'ENABLE_RCON=false',
    ]

    cmd = [
        'docker', 'create',
        '--name', container_name,
        '--label', 'managed_by=mc_manager',
        '--restart', 'no',
        '-i',
        '-p', f'127.0.0.1:{internal_port}:25565',
        '-v', f'{data_path}:/data',
    ]

    for env in env_vars:
        cmd.extend(['-e', env])

    cmd.append('itzg/minecraft-server:latest')

    print(f"    Creating container {container_name}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    ERROR: {result.stderr.strip()}")
        return False
    print(f"    Container created: {result.stdout.strip()[:12]}")
    return True


def load_crafty_notifications():
    """Load notification settings from existing Crafty config."""
    try:
        with open(CRAFTY_CONFIG_PATH, 'r') as f:
            config = json.load(f)
        return config.get('notifications', {})
    except Exception:
        return {}


def pull_mc_image():
    """Pull the itzg/minecraft-server Docker image."""
    print("\nPulling itzg/minecraft-server:latest...")
    result = subprocess.run(
        ['docker', 'pull', 'itzg/minecraft-server:latest'],
        capture_output=False
    )
    if result.returncode != 0:
        print("WARNING: Failed to pull image. Containers may fail to create.")
        return False
    return True


def main():
    print("=" * 60)
    print("  MC Server Manager - Migration from Crafty Controller")
    print("=" * 60)

    # Discover servers
    servers = discover_servers()
    if not servers:
        print("\nNo servers found in Crafty directory.")
        sys.exit(1)

    print(f"\nFound {len(servers)} server(s)")

    # Prompt for names
    servers = prompt_server_names(servers)

    # Confirm
    print("\n=== Migration Plan ===\n")
    internal_port = INTERNAL_PORT_START
    for srv in servers:
        container_name = sanitize_container_name(srv['name'])
        srv['container_name'] = container_name
        srv['internal_port'] = internal_port
        print(f"  {srv['name']}")
        print(f"    Port: {srv['port']} -> internal {internal_port}")
        print(f"    Type: {srv['type']} {srv['version']} ({srv['memory']} RAM)")
        print(f"    Container: {container_name}")
        print(f"    Data: mc_data/{container_name}/")
        print()
        internal_port += 1

    confirm = input("Proceed with migration? [y/N] ").strip().lower()
    if confirm != 'y':
        print("Aborted.")
        sys.exit(0)

    # Pull Docker image
    pull_mc_image()

    # Process each server
    print("\n=== Migrating Servers ===\n")
    for srv in servers:
        container_name = srv['container_name']
        data_path = os.path.join(MC_DATA_DIR, container_name)

        print(f"\n--- {srv['name']} (port {srv['port']}) ---")

        # Copy data
        print("  Copying data...")
        copy_server_data(srv, data_path)

        # Create container
        print("  Creating Docker container...")
        if not create_docker_container(srv, container_name, srv['internal_port'], data_path):
            print(f"  WARNING: Failed to create container for {srv['name']}")

    # Generate config.json
    print("\n=== Generating config.json ===")

    # Load existing notification settings
    notifications = load_crafty_notifications()

    config = {
        'timeout': 5,
        'auto_shutdown': True,
        'servers': [],
        'notifications': notifications if notifications else {
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
                    'player_leave': False
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
                    'player_leave': False
                }
            }
        }
    }

    for srv in servers:
        config['servers'].append({
            'name': srv['name'],
            'container_name': srv['container_name'],
            'external_port': srv['port'],
            'internal_port': srv['internal_port'],
            'type': srv['type'],
            'version': srv['version'],
            'memory': srv['memory'],
        })

    os.makedirs(os.path.dirname(OUTPUT_CONFIG_PATH), exist_ok=True)
    with open(OUTPUT_CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)
    print(f"  Written to {OUTPUT_CONFIG_PATH}")

    # Load existing Crafty config to preserve timeout
    try:
        with open(CRAFTY_CONFIG_PATH, 'r') as f:
            old_config = json.load(f)
        config['timeout'] = old_config.get('timeout', 5)
        config['auto_shutdown'] = old_config.get('auto_shutdown', True)
        with open(OUTPUT_CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception:
        pass

    print("\n" + "=" * 60)
    print("  Migration complete!")
    print("=" * 60)
    print(f"\n  Servers: {len(servers)}")
    print(f"  Config:  {OUTPUT_CONFIG_PATH}")
    print(f"  Data:    {MC_DATA_DIR}/")
    print()
    print("  Next steps:")
    print("    cd ~/minecraftserver")
    print("    docker compose up -d --build")
    print("    # Visit http://localhost:8080")
    print()
    print("  Your ~/crafty directory is untouched for rollback.")
    print()


if __name__ == '__main__':
    main()
