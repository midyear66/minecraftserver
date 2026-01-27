# Minecraft Server Manager

A Docker-based platform for managing multiple Minecraft servers with a protocol-aware proxy and web admin panel.

## Features

- **Protocol-Aware Proxy** -- Distinguishes between status pings and login attempts. Servers start on-demand when a player connects and shut down automatically after a configurable idle timeout.
- **Web Admin Panel** -- Dashboard for managing servers, viewing console output, managing players, configuring notifications, and handling backups.
- **Multi-Server Support** -- Run multiple Minecraft servers (Vanilla, Paper, Spigot, Fabric, Forge) on different ports, each in an isolated Docker container.
- **Notifications** -- Pushover and email notifications for server start/stop and player join/leave events, configured through the admin UI.
- **Backup Management** -- Create, schedule, and restore server backups through the admin panel.
- **Crafty Controller Migration** -- Import existing servers from Crafty Controller using the included migration script.

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
   git clone <repo-url>
   cd minecraftserver
   ```

2. **Configure environment:**
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and set your admin credentials and the absolute path to your `mc_data` directory.

3. **Start the services:**
   ```bash
   docker compose up -d --build
   ```

4. **Open the admin panel** at `http://localhost:8080` and log in with your configured credentials.

5. **Create your first server** using the form on the dashboard.

6. **Configure notifications** (optional) from the Settings page in the admin panel.

> **Note:** The proxy service will restart automatically until your first server is created. This is normal -- once a server exists, the proxy stays running.

## Configuration

### Environment Variables (`.env`)

| Variable | Description | Example |
|----------|-------------|---------|
| `ADMIN_USERNAME` | Admin panel login username | `admin` |
| `ADMIN_PASSWORD` | Admin panel login password | `changeme` |
| `HOST_DATA_DIR` | Absolute path to `mc_data` on the host | `/home/user/minecraftserver/mc_data` |

### Server and Notification Settings

Server configuration and notification settings are managed entirely through the admin panel UI. The settings are stored in `proxy/config.json` (auto-created on first use). See `proxy/config.json.example` for the configuration schema.

## Project Structure

```
minecraftserver/
├── admin/                  # Flask web admin panel
│   ├── app.py              # Main application
│   ├── backup_manager.py   # Backup create/restore/schedule
│   ├── templates/          # HTML templates
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
├── migrate.py              # Crafty Controller migration script
└── .env.example            # Environment variable template
```

## Migration from Crafty Controller

To import existing servers from a Crafty Controller installation:

```bash
python3 migrate.py
```

This scans `~/crafty/docker/servers/`, copies server data to `mc_data/`, and generates `proxy/config.json`. Run on the host machine, not inside a container.
