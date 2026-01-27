# Plan: Bedrock Server Support & GeyserMC Crossplay

## Overview

Add two capabilities:
1. **GeyserMC Crossplay** -- A toggle on Paper/Spigot servers that installs GeyserMC + Floodgate plugins, allowing Bedrock clients to join Java servers
2. **Standalone Bedrock Servers** -- A new `BEDROCK` server type using `itzg/minecraft-bedrock-server` Docker image with UDP protocol

Both require a new **UDP proxy** in `mc_proxy.py` to support Bedrock's RakNet/UDP protocol for wake-on-connect, status pings, and packet forwarding.

---

## Phase 1: Config Schema & Admin Backend (`admin/app.py`)

### 1a. Add BEDROCK server type

- Add `'BEDROCK'` to `VALID_SERVER_TYPES`
- Add `BEDROCK_ENV_DEFAULTS` dict for Bedrock-specific env vars:
  ```python
  BEDROCK_ENV_DEFAULTS = {
      'SERVER_NAME': 'Bedrock Server',
      'GAMEMODE': 'survival',
      'DIFFICULTY': 'easy',
      'MAX_PLAYERS': 10,
      'ALLOW_CHEATS': False,
      'VIEW_DISTANCE': 32,
      'TICK_DISTANCE': 4,
      'DEFAULT_PLAYER_PERMISSION_LEVEL': 'member',
      'TEXTUREPACK_REQUIRED': False,
      'ONLINE_MODE': True,
      'LEVEL_NAME': 'Bedrock level',
      'LEVEL_SEED': '',
      'LEVEL_TYPE': 'DEFAULT',
  }
  ```
- Add `BEDROCK_BOOLEAN_ENV_VARS` and `BEDROCK_INTEGER_ENV_VARS` sets

### 1b. Update `create_mc_container()`

Branch on server type:

**Bedrock servers:**
- Use image `itzg/minecraft-bedrock-server:latest`
- Map port `19132/udp` instead of `25565/tcp`
- Set Bedrock-specific env vars (`SERVER_NAME`, `GAMEMODE`, etc. -- the itzg Bedrock image uses these)
- Set `EULA=TRUE` (Bedrock image also needs this)

**Java servers with crossplay enabled:**
- Same `itzg/minecraft-server` image
- Add `PLUGINS` env var with GeyserMC + Floodgate download URLs
- Map additional UDP port: `19132/udp` -> bedrock_internal_port
- Container gets both TCP and UDP port mappings

### 1c. Update `create_server()` route

- Accept `bedrock_port` and `crossplay` form fields
- If type is `BEDROCK`: store `edition: "bedrock"` in config, use the port as UDP
- If crossplay enabled on Paper/Spigot: assign `bedrock_port` + `bedrock_internal_port`, store `crossplay: true`
- Validate bedrock_port not already in use (check both `external_port` and `bedrock_port` across all servers)

### 1d. Bedrock version fetcher

Add `_fetch_bedrock_versions()`:
- The itzg/minecraft-bedrock-server image accepts `VERSION=LATEST` or specific version strings
- Offer `LATEST` plus recent known versions, or just `LATEST` and `PREVIOUS` since Bedrock doesn't have a clean public version API
- Register in the `fetchers` dict

### 1e. Update `update_server()` route

- Handle Bedrock-specific env vars when saving edits
- Handle crossplay toggle changes (add/remove GeyserMC plugins, add/remove UDP port mapping)
- When crossplay toggled on: assign bedrock_port + bedrock_internal_port, recreate container
- When crossplay toggled off: remove those fields, recreate container

### 1f. GeyserMC plugin constants

```python
GEYSER_PLUGIN_URL = 'https://download.geysermc.org/v2/projects/geyser/versions/latest/builds/latest/downloads/spigot'
FLOODGATE_PLUGIN_URL = 'https://download.geysermc.org/v2/projects/floodgate/versions/latest/builds/latest/downloads/spigot'
```

When crossplay is enabled, set env: `PLUGINS={GEYSER_URL}\n{FLOODGATE_URL}`

---

## Phase 2: Templates

### 2a. Dashboard create form (`dashboard.html`)

- BEDROCK already appears in Type dropdown (populated from `server_types`)
- Add crossplay checkbox (visible only when Paper/Spigot selected):
  ```html
  <div class="form-group" id="crossplay-group" style="display: none;">
      <div class="checkbox-group">
          <input type="checkbox" id="crossplay" name="crossplay">
          <label for="crossplay">Enable Crossplay (Bedrock clients)</label>
      </div>
  </div>
  ```
- Add Bedrock port field (visible when BEDROCK selected or crossplay checked):
  ```html
  <div class="form-group" id="bedrock-port-group" style="display: none;">
      <label for="bedrock_port">Bedrock Port (UDP)</label>
      <input type="number" id="bedrock_port" name="bedrock_port"
             placeholder="19132" min="1" max="65535">
  </div>
  ```
- JavaScript: show/hide fields based on type + crossplay state
- When BEDROCK selected: show bedrock port (required), hide crossplay, version dropdown fetches Bedrock versions
- When PAPER/SPIGOT selected: show crossplay checkbox; if checked, show bedrock port field
- When VANILLA/FABRIC/FORGE selected: hide crossplay and bedrock port

### 2b. Dashboard server table (`dashboard.html`)

- Show port info distinguishing TCP vs UDP
- For Bedrock: show port with `(UDP)` suffix
- For crossplay: show both ports, e.g. `25565 + 19132`

### 2c. Edit server page (`edit_server.html`)

For **BEDROCK** servers -- replace Java-specific cards with Bedrock cards:
- Basic Settings: Server Name, Version, Memory (same structure)
- Bedrock Gameplay: SERVER_NAME (MOTD equivalent), GAMEMODE, DIFFICULTY, MAX_PLAYERS, ALLOW_CHEATS, VIEW_DISTANCE, TICK_DISTANCE, DEFAULT_PLAYER_PERMISSION_LEVEL
- Bedrock World: LEVEL_NAME, LEVEL_SEED, LEVEL_TYPE, TEXTUREPACK_REQUIRED, ONLINE_MODE
- Hide Java-specific cards (Gameplay, World, Performance, Paper Settings)

For **PAPER/SPIGOT** servers -- add crossplay section:
- New card "Crossplay (Bedrock)" with toggle + bedrock port field
- Below the Paper Settings card

### 2d. API versions endpoint

Update `/api/versions/<server_type>` to handle `BEDROCK` type.

---

## Phase 3: UDP Proxy (`proxy/mc_proxy.py`)

### 3a. RakNet protocol constants

```python
# RakNet packet IDs
RAKNET_UNCONNECTED_PING = 0x01
RAKNET_UNCONNECTED_PONG = 0x1C
RAKNET_OPEN_CONNECTION_REQUEST_1 = 0x05
RAKNET_OPEN_CONNECTION_REPLY_1 = 0x06
RAKNET_OPEN_CONNECTION_REQUEST_2 = 0x07
RAKNET_OPEN_CONNECTION_REPLY_2 = 0x08

# 16-byte magic sequence used in RakNet offline messages
RAKNET_MAGIC = b'\x00\xff\xff\x00\xfe\xfe\xfe\xfe\xfd\xfd\xfd\xfd\x12\x34\x56\x78'
```

### 3b. RakNet helper functions

- `parse_unconnected_ping(data)` -- Extract timestamp and client GUID
- `build_unconnected_pong(ping_time, server_guid, motd, protocol_version, mc_version, players, max_players, port)` -- Build synthetic pong with MOTD string in Bedrock format:
  ```
  MCPE;{motd};{protocol};{version};{players};{max_players};{guid};{level};{gamemode};1;{port};{port};
  ```
- `is_offline_raknet(data)` -- Check if packet contains magic bytes (offline message)

### 3c. UDP listener

```python
def start_udp_listener(port, config, docker_mgr):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('0.0.0.0', port))
    print(f"UDP Listening on port {port}")

    sessions = {}  # (client_ip, client_port) -> UDPSession

    while True:
        data, addr = sock.recvfrom(4096)
        handle_udp_packet(sock, data, addr, port, config, docker_mgr, sessions)
```

### 3d. Status ping handling (Unconnected Ping/Pong)

When receiving packet ID `0x01`:
- If server is running: forward ping to backend UDP port, relay pong back to client
- If server is stopped: respond with synthetic pong showing "Server sleeping - connect to wake"

### 3e. Wake-on-connect

When receiving `0x05` (Open Connection Request 1):
- If server is stopped: start Docker container, wait for ready (UDP probe)
- If server is starting: wait for ready
- Once ready: forward the packet to backend and begin session forwarding

### 3f. UDP session forwarding

After the initial connection request, create a "session":
```python
class UDPSession:
    def __init__(self, client_addr, backend_port, proxy_sock):
        self.client_addr = client_addr
        self.backend_addr = ('127.0.0.1', backend_port)
        self.backend_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.proxy_sock = proxy_sock
        self.last_activity = time.time()
        # Start backend->client forwarding thread
        self.forward_thread = threading.Thread(target=self._forward_from_backend, daemon=True)
        self.forward_thread.start()

    def send_to_backend(self, data):
        self.backend_sock.sendto(data, self.backend_addr)
        self.last_activity = time.time()

    def _forward_from_backend(self):
        """Read from backend socket, forward to client via proxy socket"""
        while True:
            try:
                self.backend_sock.settimeout(30)
                data, _ = self.backend_sock.recvfrom(4096)
                self.proxy_sock.sendto(data, self.client_addr)
                self.last_activity = time.time()
            except socket.timeout:
                if time.time() - self.last_activity > 60:
                    break
            except:
                break
```

- Each client gets a unique backend socket (so responses route correctly)
- Session expires after 60s of inactivity
- Cleanup thread periodically removes stale sessions

### 3g. Bedrock connection tracking

Track connections for idle shutdown:
- Increment on Open Connection Request 2 (0x07) -- indicates a client completing connection
- Decrement when session expires (no traffic for 60s)
- Same idle timeout / auto-shutdown logic as Java

### 3h. Update `wait_for_server_ready()` in DockerManager

Add UDP probe for Bedrock servers:
```python
if server.get('type') == 'BEDROCK' or server.get('edition') == 'bedrock':
    # Send Unconnected Ping to backend UDP port, wait for Pong
    probe_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe_sock.settimeout(2)
    ping = build_unconnected_ping()
    probe_sock.sendto(ping, ('127.0.0.1', internal_port))
    try:
        data, _ = probe_sock.recvfrom(4096)
        if data[0] == RAKNET_UNCONNECTED_PONG:
            return True
    except socket.timeout:
        pass
```

Also check Docker health check status first (same as Java path).

### 3i. Update `main()` to start both TCP and UDP listeners

For each server in config:
- If `type == 'BEDROCK'`: start UDP listener on `external_port`
- Else: start TCP listener on `external_port`
- If `crossplay == true` and `bedrock_port` exists: also start UDP listener on `bedrock_port`

### 3j. Update port lookup

`get_server_by_port()` needs to also match `bedrock_port` field, so the UDP listener for a crossplay server's Bedrock port can find the right server config. Add a `get_server_by_bedrock_port()` method or update existing method to check both fields.

---

## Phase 4: Players Page Compatibility

### 4a. Bedrock player management

Bedrock Dedicated Server uses different files:
- `allowlist.json` instead of `whitelist.json`
- `permissions.json` instead of `ops.json`
- No `banned-players.json` (bans are per-session or via addons)

**Approach**: For the initial implementation, show a notice on the Players page for Bedrock servers: "Player management for Bedrock servers is not yet supported." The Console page still works for sending commands directly.

This can be enhanced later to support Bedrock-specific player files.

---

## Phase 5: No Infrastructure Changes Needed

- **Proxy Dockerfile**: No changes (stdlib sockets handle UDP)
- **Docker Compose**: No changes (proxy uses `network_mode: host`, all ports accessible)
- **Admin Dockerfile**: No changes

---

## File Change Summary

| File | Changes |
|------|---------|
| `admin/app.py` | BEDROCK type + defaults, crossplay fields, container creation branching, GeyserMC plugin URLs, Bedrock version fetcher, edit route for Bedrock env vars, create route for bedrock_port/crossplay |
| `admin/templates/dashboard.html` | Crossplay checkbox, Bedrock port field, JS show/hide, port display in table |
| `admin/templates/edit_server.html` | Bedrock settings cards (conditional), crossplay toggle for Paper/Spigot |
| `admin/templates/players.html` | "Not supported" notice for Bedrock servers |
| `proxy/mc_proxy.py` | RakNet constants/helpers, UDP listener, Bedrock ping/pong, wake-on-connect, UDP session forwarding, connection tracking, wait_for_ready UDP probe |

No new files. No deleted files.

---

## Implementation Order

1. `admin/app.py` -- Backend logic (BEDROCK type, container creation, crossplay config)
2. `admin/templates/dashboard.html` -- Create form UI
3. `admin/templates/edit_server.html` -- Edit form with Bedrock cards
4. `admin/templates/players.html` -- Bedrock notice
5. `proxy/mc_proxy.py` -- UDP proxy (largest change)
6. Testing -- Create Bedrock server, test wake-on-connect, test crossplay

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| UDP proxy complexity (connectionless, session tracking) | Clean session model with per-client backend sockets and timeout-based cleanup |
| RakNet protocol edge cases | Only handle offline messages (ping/pong, connection request); forward all other packets transparently |
| GeyserMC version compatibility | Use `latest` build URLs which auto-match recent MC versions |
| Bedrock version API unavailable | Offer `LATEST` only (or `LATEST` + `PREVIOUS`); users rarely pin Bedrock versions |
| Player tracking for idle shutdown | Use session timeout (no UDP traffic = player gone); periodic backend pings as fallback |
| Bedrock player management differs | Defer to Phase 2 enhancement; Console page works for all command needs |
