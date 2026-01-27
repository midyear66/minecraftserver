#!/usr/bin/env python3
"""
Minecraft Protocol-Aware Proxy with Direct Docker Management

This proxy distinguishes between status pings and login attempts:
- Status ping (next_state=1): Returns fake MOTD, doesn't start server
- Login attempt (next_state=2): Starts server via Docker, then proxies
"""

import socket
import struct
import threading
import json
import time
import os
from datetime import datetime
import docker
from typing import Optional, Tuple
from notifications import NotificationManager

# Configuration
CONFIG_PATH = '/app/config/config.json'
LOGS_DIR = '/app/logs'


# ============== Usage Logging ==============

class UsageLogger:
    """Logs server usage events to daily JSON-lines files"""

    def __init__(self, logs_dir: str = LOGS_DIR):
        self.logs_dir = logs_dir
        self._lock = threading.Lock()

    def _get_log_path(self) -> str:
        """Get today's log file path"""
        today = datetime.now().strftime('%Y-%m-%d')
        return os.path.join(self.logs_dir, f'usage-{today}.log')

    def _write_event(self, event: dict):
        """Write an event to the log file (creates file only if writing)"""
        event['timestamp'] = datetime.now().isoformat()
        with self._lock:
            try:
                os.makedirs(self.logs_dir, exist_ok=True)
                with open(self._get_log_path(), 'a') as f:
                    f.write(json.dumps(event) + '\n')
            except Exception as e:
                print(f"Error writing to usage log: {e}")

    def log_server_start(self, port: int, server_name: str = None):
        """Log a server start event"""
        self._write_event({
            'event': 'server_start',
            'port': port,
            'server_name': server_name
        })

    def log_server_stop(self, port: int, reason: str, server_name: str = None):
        """Log a server stop event"""
        self._write_event({
            'event': 'server_stop',
            'port': port,
            'server_name': server_name,
            'reason': reason
        })

    def log_player_join(self, port: int, player_count: int, server_name: str = None, player_name: str = None):
        """Log a player join event"""
        self._write_event({
            'event': 'player_join',
            'port': port,
            'server_name': server_name,
            'players': player_count,
            'player_name': player_name
        })

    def log_player_leave(self, port: int, player_count: int, server_name: str = None, player_name: str = None):
        """Log a player leave event"""
        self._write_event({
            'event': 'player_leave',
            'port': port,
            'server_name': server_name,
            'players': player_count,
            'player_name': player_name
        })


# Global usage logger
usage_logger = UsageLogger()

# Global notification manager (initialized in main)
notification_manager = None

# Global state
server_connections = {}  # port -> count of active connections
server_states = {}  # port -> 'stopped' | 'starting' | 'running'
shutdown_timers = {}  # port -> Timer object
state_lock = threading.Lock()


def load_config() -> dict:
    """Load configuration from config.json"""
    try:
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading config: {e}")
        return {}


# ============== Minecraft Protocol Helpers ==============

def read_varint(sock: socket.socket) -> Tuple[int, bytes]:
    """Read a VarInt from socket, return (value, raw_bytes)"""
    result = 0
    raw = b''
    for i in range(5):
        try:
            byte = sock.recv(1)
            if not byte:
                raise ConnectionError("Connection closed")
            raw += byte
            b = byte[0]
            result |= (b & 0x7F) << (7 * i)
            if not (b & 0x80):
                break
        except:
            raise
    return result, raw


def read_varint_from_buffer(data: bytes, offset: int = 0) -> Tuple[int, int]:
    """Read a VarInt from bytes buffer, return (value, bytes_read)"""
    result = 0
    for i in range(5):
        if offset + i >= len(data):
            raise ValueError("Buffer too short for VarInt")
        b = data[offset + i]
        result |= (b & 0x7F) << (7 * i)
        if not (b & 0x80):
            return result, i + 1
    raise ValueError("VarInt too long")


def write_varint(value: int) -> bytes:
    """Encode an integer as VarInt"""
    result = b''
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            result += bytes([byte | 0x80])
        else:
            result += bytes([byte])
            break
    return result


def read_string_from_buffer(data: bytes, offset: int = 0) -> Tuple[str, int]:
    """Read a Minecraft string (VarInt length + UTF-8), return (string, bytes_read)"""
    length, varint_size = read_varint_from_buffer(data, offset)
    string_start = offset + varint_size
    string_end = string_start + length
    if string_end > len(data):
        raise ValueError("Buffer too short for string")
    return data[string_start:string_end].decode('utf-8'), varint_size + length


def write_string(s: str) -> bytes:
    """Encode a string as Minecraft string (VarInt length + UTF-8)"""
    encoded = s.encode('utf-8')
    return write_varint(len(encoded)) + encoded


def parse_handshake(data: bytes) -> dict:
    """Parse a Minecraft handshake packet"""
    offset = 0

    # Packet ID (should be 0x00)
    packet_id, size = read_varint_from_buffer(data, offset)
    offset += size

    # Protocol version
    protocol_version, size = read_varint_from_buffer(data, offset)
    offset += size

    # Server address
    server_address, size = read_string_from_buffer(data, offset)
    offset += size

    # Server port (unsigned short, big-endian)
    server_port = struct.unpack('>H', data[offset:offset+2])[0]
    offset += 2

    # Next state
    next_state, size = read_varint_from_buffer(data, offset)

    return {
        'packet_id': packet_id,
        'protocol_version': protocol_version,
        'server_address': server_address,
        'server_port': server_port,
        'next_state': next_state  # 1 = status, 2 = login
    }


def parse_login_start(data: bytes) -> str:
    """Parse a Login Start packet and return the player name"""
    offset = 0

    # Packet ID (should be 0x00)
    packet_id, size = read_varint_from_buffer(data, offset)
    offset += size

    # Player name
    player_name, _ = read_string_from_buffer(data, offset)

    return player_name


def build_status_response(motd: str, protocol_version: int = 765, max_players: int = 20, online: int = 0) -> bytes:
    """Build a status response packet"""
    status = {
        "version": {
            "name": "1.20.4",
            "protocol": protocol_version
        },
        "players": {
            "max": max_players,
            "online": online,
            "sample": []
        },
        "description": {
            "text": motd
        },
        "enforcesSecureChat": False,
        "previewsChat": False
    }

    json_str = json.dumps(status)

    # Build packet: packet_id (0x00) + json string
    packet_data = write_varint(0x00) + write_string(json_str)

    # Prepend packet length
    return write_varint(len(packet_data)) + packet_data


def build_ping_response(payload: int) -> bytes:
    """Build a ping response packet"""
    # Packet ID 0x01 + long payload
    packet_data = write_varint(0x01) + struct.pack('>q', payload)
    return write_varint(len(packet_data)) + packet_data


def build_disconnect_packet(reason: str) -> bytes:
    """Build a disconnect packet for login state"""
    # Packet ID 0x00 for disconnect during login
    reason_json = json.dumps({"text": reason})
    packet_data = write_varint(0x00) + write_string(reason_json)
    return write_varint(len(packet_data)) + packet_data


# ============== Docker Manager ==============

class DockerManager:
    """Manages Minecraft server containers via Docker SDK"""

    def __init__(self, config: dict):
        self.config = config
        self.client = docker.from_env()

    def get_server_by_port(self, port: int) -> Optional[dict]:
        """Find a server config entry by its external port"""
        for server in self.config.get('servers', []):
            if int(server.get('external_port', 0)) == int(port):
                return server
        return None

    def is_server_running(self, port: int) -> bool:
        """Check if a server's container is running"""
        server = self.get_server_by_port(port)
        if not server:
            return False
        try:
            container = self.client.containers.get(server['container_name'])
            return container.status == 'running'
        except docker.errors.NotFound:
            return False
        except Exception as e:
            print(f"Error checking server status: {e}")
            return False

    def start_server(self, port: int) -> bool:
        """Start a server's container"""
        server = self.get_server_by_port(port)
        if not server:
            print(f"No server found on port {port}")
            return False
        try:
            container = self.client.containers.get(server['container_name'])
            container.start()
            return True
        except Exception as e:
            print(f"Error starting server: {e}")
            return False

    def stop_server(self, port: int) -> bool:
        """Stop a server's container (timeout=30 for graceful shutdown)"""
        server = self.get_server_by_port(port)
        if not server:
            return False
        try:
            container = self.client.containers.get(server['container_name'])
            container.stop(timeout=30)
            return True
        except Exception as e:
            print(f"Error stopping server: {e}")
            return False

    def wait_for_server_ready(self, port: int, timeout: int = 120) -> bool:
        """Wait for server to be ready to accept connections.

        Uses Docker health check status from itzg/minecraft-server if
        available, falling back to a TCP connection check for images
        without a health check.
        """
        server = self.get_server_by_port(port)
        if not server:
            return False

        internal_port = int(server['internal_port'])
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                container = self.client.containers.get(server['container_name'])
                if container.status != 'running':
                    time.sleep(2)
                    continue

                # Check Docker health check status (itzg/minecraft-server
                # reports 'healthy' when the MC server is truly ready)
                health = container.attrs.get('State', {}).get('Health', {}).get('Status', '')
                if health == 'healthy':
                    return True
                if health in ('starting', 'unhealthy'):
                    # Health check exists but not passing yet — keep waiting
                    time.sleep(2)
                    continue

                # No health check configured — fall back to TCP probe
                try:
                    test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    test_sock.settimeout(2)
                    test_sock.connect(('127.0.0.1', internal_port))
                    test_sock.close()
                    return True
                except:
                    pass
            except:
                pass
            time.sleep(2)
        return False


# ============== Connection Tracking ==============

def increment_connections(port: int, docker_mgr: DockerManager, player_name: str = None):
    """Increment connection count for a port"""
    with state_lock:
        server_connections[port] = server_connections.get(port, 0) + 1
        count = server_connections[port]
        # Cancel any pending shutdown timer
        if port in shutdown_timers:
            shutdown_timers[port].cancel()
            del shutdown_timers[port]
        print(f"[Port {port}] Connections: {count} (player: {player_name})")

    # Log player join (outside lock to avoid blocking)
    server = docker_mgr.get_server_by_port(port)
    name = server.get('name') if server else f'Port {port}'
    usage_logger.log_player_join(port, count, name, player_name)

    # Send notification
    if notification_manager and player_name:
        notification_manager.notify('player_join', player=player_name, name=name, count=count)


def decrement_connections(port: int, config: dict, docker_mgr: DockerManager, player_name: str = None):
    """Decrement connection count and schedule shutdown if empty"""
    # Get server name for logging (outside lock)
    server = docker_mgr.get_server_by_port(port)
    name = server.get('name') if server else f'Port {port}'

    with state_lock:
        server_connections[port] = max(0, server_connections.get(port, 0) - 1)
        count = server_connections[port]
        print(f"[Port {port}] Connections: {count} (player left: {player_name})")

        if count == 0 and config.get('auto_shutdown', True):
            timeout_minutes = config.get('timeout', 5)
            print(f"[Port {port}] Scheduling shutdown in {timeout_minutes} minutes")

            def do_shutdown():
                should_stop = False
                with state_lock:
                    if server_connections.get(port, 0) == 0:
                        print(f"[Port {port}] Shutting down server (idle timeout)")
                        server_states[port] = 'stopped'
                        should_stop = True
                # Stop container outside the lock (can take up to 30s)
                if should_stop:
                    docker_mgr.stop_server(port)
                    usage_logger.log_server_stop(port, 'idle_timeout', name)
                    if notification_manager:
                        notification_manager.notify('server_stop', name=name, reason='idle timeout')

            timer = threading.Timer(timeout_minutes * 60, do_shutdown)
            shutdown_timers[port] = timer
            timer.start()

    # Log player leave (outside lock)
    usage_logger.log_player_leave(port, count, name, player_name)

    # Send notification
    if notification_manager and player_name:
        notification_manager.notify('player_leave', player=player_name, name=name, count=count)


# ============== Proxy Logic ==============

def proxy_data(client: socket.socket, server: socket.socket):
    """Bidirectionally proxy data between client and server"""
    def forward(src, dst, label):
        try:
            while True:
                data = src.recv(4096)
                if not data:
                    break
                dst.sendall(data)
        except:
            pass
        finally:
            try:
                src.shutdown(socket.SHUT_RD)
            except:
                pass
            try:
                dst.shutdown(socket.SHUT_WR)
            except:
                pass

    t1 = threading.Thread(target=forward, args=(client, server, "c->s"))
    t2 = threading.Thread(target=forward, args=(server, client, "s->c"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()


def handle_status_request(client: socket.socket, handshake: dict, handshake_raw: bytes, docker_mgr: DockerManager):
    """Handle a status (server list) ping.

    If the server is running, proxy the request to get the real MOTD,
    player count, and icon. Otherwise return a 'sleeping' message.
    """
    port = handshake['server_port']
    server_info = docker_mgr.get_server_by_port(port)

    try:
        # Check if we can proxy to the real server
        if server_info and docker_mgr.is_server_running(port):
            internal_port = int(server_info['internal_port'])
            try:
                backend = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                backend.settimeout(5)
                backend.connect(('127.0.0.1', internal_port))

                # Forward handshake
                backend.sendall(handshake_raw)

                # Read and forward status request from client
                packet_len, packet_len_raw = read_varint(client)
                packet_data = client.recv(packet_len)
                backend.sendall(packet_len_raw + packet_data)

                # Read status response from server and forward to client
                resp_len, resp_len_raw = read_varint(backend)
                resp_data = backend.recv(resp_len)
                client.sendall(resp_len_raw + resp_data)

                # Handle ping/pong
                try:
                    client.settimeout(2.0)
                    ping_len, ping_len_raw = read_varint(client)
                    ping_data = client.recv(ping_len)
                    backend.sendall(ping_len_raw + ping_data)

                    pong_len, pong_len_raw = read_varint(backend)
                    pong_data = backend.recv(pong_len)
                    client.sendall(pong_len_raw + pong_data)
                except socket.timeout:
                    pass

                backend.close()
                return
            except Exception:
                # Server not reachable, fall through to sleeping message
                try:
                    backend.close()
                except:
                    pass

        # Server not running — return sleeping message
        packet_len, _ = read_varint(client)
        packet_data = client.recv(packet_len)

        motd = "\u00a77Server is sleeping. \u00a7aConnect to wake it up!"
        response = build_status_response(motd, handshake['protocol_version'])
        client.sendall(response)

        try:
            client.settimeout(2.0)
            packet_len, _ = read_varint(client)
            packet_data = client.recv(packet_len)

            if len(packet_data) >= 9:
                payload = struct.unpack('>q', packet_data[1:9])[0]
                pong = build_ping_response(payload)
                client.sendall(pong)
        except socket.timeout:
            pass
    except Exception as e:
        print(f"Error handling status: {e}")
    finally:
        client.close()


def handle_login_request(client: socket.socket, handshake: dict, handshake_raw: bytes, config: dict, docker_mgr: DockerManager):
    """Handle a login request - start server and proxy"""
    port = handshake['server_port']

    # Get server info for logging
    server_info = docker_mgr.get_server_by_port(port)
    name = server_info.get('name') if server_info else None

    if not server_info:
        print(f"[Port {port}] No server configured for this port")
        try:
            client.sendall(build_disconnect_packet("No server configured for this port."))
            client.close()
        except:
            pass
        return

    internal_port = int(server_info['internal_port'])

    # Read the Login Start packet to get player name
    player_name = None
    login_raw = b''
    try:
        login_len, login_len_raw = read_varint(client)
        login_data = client.recv(login_len)
        login_raw = login_len_raw + login_data
        player_name = parse_login_start(login_data)
        print(f"[Port {port}] Player '{player_name}' connecting")
    except Exception as e:
        print(f"[Port {port}] Failed to parse login packet: {e}")
        # Reject malformed login attempts (likely scanners/bots)
        try:
            client.sendall(build_disconnect_packet("Invalid login packet"))
            client.close()
        except:
            pass
        return

    try:
        # Check actual Docker status (in-memory state can be stale if
        # the server was stopped via admin UI or crashed)
        actually_running = docker_mgr.is_server_running(port)

        with state_lock:
            state = server_states.get(port, 'stopped')
            # Sync in-memory state with reality
            if actually_running and state == 'stopped':
                server_states[port] = 'running'
                state = 'running'
            elif not actually_running and state == 'running':
                server_states[port] = 'stopped'
                state = 'stopped'

        if state == 'stopped':
            # Start the server
            print(f"[Port {port}] Starting server for login request")
            with state_lock:
                server_states[port] = 'starting'

            if not docker_mgr.start_server(port):
                print(f"[Port {port}] Failed to start server container")
                client.sendall(build_disconnect_packet("Failed to start server. Please try again."))
                client.close()
                with state_lock:
                    server_states[port] = 'stopped'
                return

            # Wait for server to be ready
            if not docker_mgr.wait_for_server_ready(port, timeout=120):
                print(f"[Port {port}] Server failed to start in time")
                client.sendall(build_disconnect_packet("Server failed to start. Please try again."))
                client.close()
                with state_lock:
                    server_states[port] = 'stopped'
                return

            with state_lock:
                server_states[port] = 'running'

            # Log successful server start
            usage_logger.log_server_start(port, name)

            # Send notification
            if notification_manager:
                notification_manager.notify('server_start', name=name or f'Port {port}', port=port)

        elif state == 'starting':
            # Wait for it to finish starting
            if not docker_mgr.wait_for_server_ready(port, timeout=120):
                client.sendall(build_disconnect_packet("Server is starting. Please try again."))
                client.close()
                return

        # Connect to the actual server via localhost
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.settimeout(10)
        server.connect(('127.0.0.1', internal_port))
        server.settimeout(None)

        # Forward the handshake and login packets we already received
        server.sendall(handshake_raw + login_raw)

        # Track this connection
        increment_connections(port, docker_mgr, player_name)

        try:
            # Proxy all traffic
            proxy_data(client, server)
        finally:
            decrement_connections(port, config, docker_mgr, player_name)
            try:
                server.close()
            except:
                pass

    except Exception as e:
        print(f"[Port {port}] Error handling login: {e}")
    finally:
        try:
            client.close()
        except:
            pass


def handle_client(client: socket.socket, addr, config: dict, docker_mgr: DockerManager):
    """Handle an incoming client connection"""
    try:
        client.settimeout(30.0)

        # Read the handshake packet
        packet_len, packet_len_raw = read_varint(client)
        packet_data = client.recv(packet_len)

        # Full raw handshake (length + data)
        handshake_raw = packet_len_raw + packet_data

        # Parse the handshake
        handshake = parse_handshake(packet_data)

        print(f"[{addr[0]}] Handshake: port={handshake['server_port']}, next_state={handshake['next_state']}")

        if handshake['next_state'] == 1:
            # Status request - proxy to real server if running, else show sleeping
            handle_status_request(client, handshake, handshake_raw, docker_mgr)
        elif handshake['next_state'] == 2:
            # Login request - start server and proxy
            handle_login_request(client, handshake, handshake_raw, config, docker_mgr)
        else:
            print(f"Unknown next_state: {handshake['next_state']}")
            client.close()

    except Exception as e:
        print(f"[{addr[0]}] Error: {e}")
        try:
            client.close()
        except:
            pass


def start_listener(port: int, config: dict, docker_mgr: DockerManager):
    """Start a listener on the given port"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('0.0.0.0', int(port)))
    server.listen(5)

    print(f"Listening on port {port}")

    while True:
        try:
            client, addr = server.accept()
            thread = threading.Thread(target=handle_client, args=(client, addr, config, docker_mgr))
            thread.daemon = True
            thread.start()
        except Exception as e:
            print(f"Error accepting connection on port {port}: {e}")


def main():
    global notification_manager

    print("MC Server Manager Proxy starting...")

    # Load configuration
    config = load_config()
    if not config:
        print("Failed to load configuration. Exiting.")
        return

    servers = config.get('servers', [])
    print(f"Loaded config: {len(servers)} server(s)")

    # Initialize Docker manager
    docker_mgr = DockerManager(config)

    # Initialize notification manager
    notification_manager = NotificationManager(config)
    print("Notification manager initialized")

    # Start listeners for each configured server
    threads = []
    for srv in servers:
        port = int(srv.get('external_port', 0))
        if port:
            t = threading.Thread(target=start_listener, args=(port, config, docker_mgr))
            t.daemon = True
            t.start()
            threads.append(t)

    if not threads:
        print("No servers configured. Exiting.")
        return

    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Shutting down...")


if __name__ == '__main__':
    main()
