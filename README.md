# Minecraft Server Manager

> **Note:** This project was entirely vibe-coded with [Claude](https://claude.ai). I built it for my own use to manage Minecraft servers for friends and family, but figured others might find it useful too. Expect rough edges and opinionated decisions!

A Docker-based platform for managing multiple Minecraft servers with a protocol-aware proxy and web admin panel.

## Features

- **Protocol-Aware Proxy** -- Distinguishes between status pings and login attempts. Servers start on-demand when a player connects and shut down automatically after a configurable idle timeout.
- **Web Admin Panel** -- Dashboard for managing servers, viewing console output, managing players, configuring notifications, and handling backups.
- **Multi-Server Support** -- Run multiple Minecraft servers (Vanilla, Paper, Spigot, Fabric, Forge) on different ports, each in an isolated Docker container.
- **Server Import** -- Import existing servers from ZIP/tar.gz archives or local directories. Auto-detects server type and version from JAR files and directory structure.
- **Mod/Plugin Management** -- Upload mods/plugins directly, or configure auto-download from Modrinth and SpigotMC. Schedule updates to run while servers are stopped to avoid startup delays.
- **Scheduled Tasks** -- Automate version checks, server restarts, commands, broadcasts, and mod updates on custom schedules.
- **Notifications** -- Pushover and email notifications for server start/stop, player join/leave, and unauthorized login attempts.
- **Backup Management** -- Create, schedule, and restore server backups through the admin panel.
- **Cross-Platform** -- Works on Linux, macOS (Docker Desktop), and Windows (WSL/Git Bash) with automatic platform detection and networking configuration.
- **Crafty Controller Migration** -- Import existing servers from Crafty Controller using the included migration script.
- **BlueMap Integration** -- 3D interactive web maps of your Minecraft worlds. Runs as a plugin for Paper/Spigot or as a standalone container for Vanilla/Forge/Fabric servers.

## Architecture

```
Internet --> Proxy (mc_proxy) --> Minecraft Server Containers (itzg/minecraft-server)
                 |
           Admin Panel (mc_admin:8080)
```

The proxy listens on each server's external port. When a player connects, it starts the corresponding Docker container and proxies the connection. Status pings return an MOTD without starting the server.

The admin panel provides a web interface on port 8080 for server configuration, monitoring, and management.

## Prerequisites

- Docker and Docker Compose

## Quick Start

1. **Clone the repository:**
   ```bash
   git clone https://github.com/midyear66/minecraftserver.git
   cd minecraftserver
   ```

2. **Run the start script:**
   ```bash
   ./start_mcserver
   ```
   On first run, you'll be prompted for admin credentials. The script handles everything else: platform detection, environment setup, Docker builds, and service startup.

3. **Open the admin panel** at `http://localhost:8080` and log in with your configured credentials.

4. **Create or import a server** using the dashboard.

5. **Forward the server port** (TCP) on your router if you want players to connect from outside your network.

6. **Configure notifications** (optional) from the Settings page in the admin panel.

The `start_mcserver` script supports Linux, macOS (Docker Desktop), and Windows (WSL/Git Bash). It automatically configures networking for your platform.

> **Note:** The proxy service will restart automatically until your first server is created. This is normal -- once a server exists, the proxy stays running.

## Configuration

### Environment Variables (`.env`)

The `start_mcserver` script automatically configures these on first run. For manual setup or customization:

| Variable | Description | Example |
|----------|-------------|---------|
| `ADMIN_USERNAME` | Admin panel login username | `admin` |
| `ADMIN_PASSWORD` | Admin panel login password | `changeme` |
| `HOST_DATA_DIR` | Absolute path to `mc_data` on the host | `/home/user/minecraftserver/mc_data` |
| `DOCKER_GID` | Docker group ID (Linux only, auto-detected) | `999` |

### Server and Notification Settings

Server configuration and notification settings are managed entirely through the admin panel UI. The settings are stored in `proxy/config.json` (auto-created on first use). See `proxy/config.json.example` for the configuration schema.

## Project Structure

```
minecraftserver/
├── admin/                  # Flask web admin panel
│   ├── app.py              # Main application and routes
│   ├── backup_manager.py   # Backup create/restore/schedule
│   ├── scheduler.py        # Scheduled task system (APScheduler)
│   ├── templates/          # Jinja2 HTML templates
│   ├── Dockerfile
│   └── requirements.txt
├── proxy/                  # Protocol-aware Minecraft proxy
│   ├── mc_proxy.py         # Main proxy server
│   ├── notifications.py    # Email and Pushover notifications
│   ├── config.json.example # Configuration schema reference
│   ├── Dockerfile
│   └── requirements.txt
├── mc_data/                # Minecraft server data (gitignored)
├── backups/                # Server backups (gitignored)
├── logs/                   # Usage logs (gitignored)
├── docker-compose.yaml
├── start_mcserver          # Cross-platform startup script
├── migrate.py              # Crafty Controller migration script
├── .env.example            # Environment variable template
└── FUTURE.md               # Bedrock & crossplay roadmap
```

## Scheduled Tasks

The admin panel includes a task scheduler for automating server maintenance:

| Task Type | Description |
|-----------|-------------|
| **Version Check** | Check for newer Minecraft versions. Optionally auto-update and restart. |
| **Scheduled Restart** | Stop and restart the server on a schedule. |
| **Run Command** | Execute a Minecraft command (e.g., `save-all`, `whitelist reload`). |
| **Broadcast Message** | Send an in-game message to all players. |
| **Update Mods/Plugins** | Download mods from Modrinth and plugins from Spiget. |

Tasks can use preset schedules (hourly, daily, weekly) or custom cron expressions.

## Mod/Plugin Management

For Paper, Spigot, Fabric, and Forge servers, the admin panel provides mod/plugin management:

- **Direct Upload** -- Upload `.jar` files directly to the server's mods/plugins directory.
- **Modrinth Integration** -- Search and add mods/plugins from Modrinth by project slug.
- **SpigotMC Integration** -- Add plugins by SpigotMC resource ID (Paper/Spigot only).
- **Scheduled Updates** -- Use the "Update Mods/Plugins" scheduled task to pre-download mods while the server is stopped, avoiding startup delays.

Access mod management from the "Mods" or "Plugins" button on the dashboard (only shown for modded server types).

## Importing Existing Servers

The admin panel supports importing existing Minecraft servers:

- **Archive Upload** -- Upload a ZIP or tar.gz archive containing your server files (world/, server.properties, etc.)
- **Local Path** -- Specify a path to an existing server directory on the host machine

The import process:
1. Extracts/copies files to a new container data directory
2. Auto-detects server type (Vanilla, Paper, Spigot, Fabric, Forge) from JAR files and directory structure
3. Auto-detects Minecraft version from JAR filenames or version.json
4. Creates and optionally starts the Docker container

Access the Import Server form from the dashboard, below Create Server.

## BlueMap Integration

BlueMap provides a 3D interactive web map of your Minecraft world, accessible via browser.

### Supported Server Types

| Server Type | BlueMap Mode | Description |
|-------------|--------------|-------------|
| Paper, Spigot | Plugin | Runs inside the Minecraft server. Map only accessible when server is running. |
| Vanilla, Forge, Fabric | Standalone | Runs in a separate Docker container. Map remains accessible even when server is stopped. |

### Enabling BlueMap

- **New servers:** Check "Enable BlueMap" when creating the server.
- **Existing servers:** Enable via the Edit Server page.

BlueMap auto-allocates a web interface port starting from 8100. Access the map from the "Map" button on the dashboard.

### Options

| Option | Description |
|--------|-------------|
| **Enable BlueMap** | Activates the 3D web map for this server. |
| **Show Caves** | Renders underground caves in the map. Increases render time and storage usage. |

### Notes

- Configuration files are auto-generated with `accept-download: true` to skip the manual acceptance step.
- Standalone mode reads world files externally, so the map can continue rendering and remain viewable while the Minecraft server is stopped.
- Changing the caves setting triggers a map re-render for the affected dimensions.

## Migration from Crafty Controller

To import existing servers from a Crafty Controller installation:

```bash
python3 migrate.py
```

This scans `~/crafty/docker/servers/`, copies server data to `mc_data/`, and generates `proxy/config.json`. Run on the host machine, not inside a container.

## Roadmap

See [FUTURE.md](FUTURE.md) for the planned Bedrock server support and GeyserMC crossplay implementation.
