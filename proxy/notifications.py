#!/usr/bin/env python3
"""
Notification Module for MC Server Manager

Supports Email (SMTP) and Pushover notifications with async dispatch.
"""

import smtplib
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from abc import ABC, abstractmethod
from typing import Optional
import requests


# Message templates
MESSAGE_TEMPLATES = {
    'server_start': {
        'subject': '[MC] Server Started: {name}',
        'body': 'Server "{name}" on port {port} started'
    },
    'server_stop': {
        'subject': '[MC] Server Stopped: {name}',
        'body': 'Server "{name}" stopped. Reason: {reason}'
    },
    'player_join': {
        'subject': '[MC] Player Joined: {player}',
        'body': '{player} joined "{name}". Online: {count}'
    },
    'player_leave': {
        'subject': '[MC] Player Left: {player}',
        'body': '{player} left "{name}". Online: {count}'
    }
}


class NotificationSender(ABC):
    """Base class for notification senders"""

    @abstractmethod
    def send(self, subject: str, body: str) -> bool:
        """Send a notification. Returns True on success."""
        pass

    @abstractmethod
    def test(self) -> tuple[bool, str]:
        """Test the notification configuration. Returns (success, message)."""
        pass


class EmailSender(NotificationSender):
    """SMTP email notification sender"""

    def __init__(self, config: dict):
        self.host = config.get('smtp_host', '')
        self.port = config.get('smtp_port', 587)
        self.tls = config.get('smtp_tls', True)
        self.user = config.get('smtp_user', '')
        self.password = config.get('smtp_password', '')
        self.from_address = config.get('from_address', '')
        self.to_addresses = config.get('to_addresses', [])

    def send(self, subject: str, body: str) -> bool:
        """Send an email notification"""
        if not self.host or not self.to_addresses:
            return False

        try:
            msg = MIMEMultipart()
            msg['From'] = self.from_address
            msg['To'] = ', '.join(self.to_addresses)
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain'))

            if self.tls:
                server = smtplib.SMTP(self.host, self.port, timeout=10)
                server.starttls()
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=10)

            if self.user and self.password:
                server.login(self.user, self.password)

            server.sendmail(self.from_address, self.to_addresses, msg.as_string())
            server.quit()
            return True
        except Exception as e:
            print(f"Email send error: {e}")
            return False

    def test(self) -> tuple[bool, str]:
        """Test email configuration"""
        if not self.host:
            return False, "SMTP host not configured"
        if not self.to_addresses:
            return False, "No recipient addresses configured"
        if not self.from_address:
            return False, "From address not configured"

        try:
            if self.tls:
                server = smtplib.SMTP(self.host, self.port, timeout=10)
                server.starttls()
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=10)

            if self.user and self.password:
                server.login(self.user, self.password)

            # Send test email
            msg = MIMEMultipart()
            msg['From'] = self.from_address
            msg['To'] = ', '.join(self.to_addresses)
            msg['Subject'] = '[MC] Test Notification'
            msg.attach(MIMEText('This is a test notification from MC Server Manager.', 'plain'))

            server.sendmail(self.from_address, self.to_addresses, msg.as_string())
            server.quit()
            return True, "Test email sent successfully"
        except smtplib.SMTPAuthenticationError:
            return False, "SMTP authentication failed"
        except smtplib.SMTPConnectError:
            return False, f"Could not connect to SMTP server {self.host}:{self.port}"
        except Exception as e:
            return False, f"Error: {str(e)}"


class PushoverSender(NotificationSender):
    """Pushover notification sender"""

    API_URL = "https://api.pushover.net/1/messages.json"

    def __init__(self, config: dict):
        self.user_key = config.get('user_key', '')
        self.app_token = config.get('app_token', '')
        self.priority = config.get('priority', 0)

    def send(self, subject: str, body: str) -> bool:
        """Send a Pushover notification"""
        if not self.user_key or not self.app_token:
            return False

        try:
            data = {
                'token': self.app_token,
                'user': self.user_key,
                'title': subject,
                'message': body,
                'priority': self.priority
            }
            resp = requests.post(self.API_URL, data=data, timeout=10)
            return resp.status_code == 200
        except Exception as e:
            print(f"Pushover send error: {e}")
            return False

    def test(self) -> tuple[bool, str]:
        """Test Pushover configuration"""
        if not self.user_key:
            return False, "User key not configured"
        if not self.app_token:
            return False, "App token not configured"

        try:
            data = {
                'token': self.app_token,
                'user': self.user_key,
                'title': '[MC] Test Notification',
                'message': 'This is a test notification from MC Server Manager.',
                'priority': self.priority
            }
            resp = requests.post(self.API_URL, data=data, timeout=10)

            if resp.status_code == 200:
                return True, "Test notification sent successfully"
            else:
                error = resp.json().get('errors', ['Unknown error'])
                return False, f"Pushover error: {', '.join(error)}"
        except Exception as e:
            return False, f"Error: {str(e)}"


class NotificationManager:
    """Manages notification dispatch with fire-and-forget async delivery"""

    def __init__(self, config: dict):
        self.config = config.get('notifications', {})
        self._reload_senders()

    def _reload_senders(self):
        """Reload senders from config"""
        self.email_config = self.config.get('email', {})
        self.pushover_config = self.config.get('pushover', {})

        self.email_sender = EmailSender(self.email_config) if self.email_config.get('enabled') else None
        self.pushover_sender = PushoverSender(self.pushover_config) if self.pushover_config.get('enabled') else None

    def reload_config(self, config: dict):
        """Reload configuration"""
        self.config = config.get('notifications', {})
        self._reload_senders()

    def notify(self, event: str, **kwargs):
        """
        Send notifications for an event (fire-and-forget).

        Args:
            event: One of 'server_start', 'server_stop', 'player_join', 'player_leave'
            **kwargs: Event-specific parameters (name, port, player, count, reason)
        """
        if event not in MESSAGE_TEMPLATES:
            print(f"Unknown notification event: {event}")
            return

        template = MESSAGE_TEMPLATES[event]

        # Format subject and body with provided kwargs
        try:
            subject = template['subject'].format(**kwargs)
            body = template['body'].format(**kwargs)
        except KeyError as e:
            print(f"Missing template parameter for {event}: {e}")
            return

        # Send via enabled channels in daemon threads (fire-and-forget)
        if self.email_sender and self.email_config.get('events', {}).get(event, False):
            thread = threading.Thread(target=self.email_sender.send, args=(subject, body))
            thread.daemon = True
            thread.start()

        if self.pushover_sender and self.pushover_config.get('events', {}).get(event, False):
            thread = threading.Thread(target=self.pushover_sender.send, args=(subject, body))
            thread.daemon = True
            thread.start()

    def test_email(self) -> tuple[bool, str]:
        """Test email notification"""
        if not self.email_config.get('enabled'):
            # Create a temporary sender for testing even if disabled
            sender = EmailSender(self.email_config)
            return sender.test()
        return self.email_sender.test()

    def test_pushover(self) -> tuple[bool, str]:
        """Test Pushover notification"""
        if not self.pushover_config.get('enabled'):
            # Create a temporary sender for testing even if disabled
            sender = PushoverSender(self.pushover_config)
            return sender.test()
        return self.pushover_sender.test()


# Default empty config for notifications
DEFAULT_NOTIFICATIONS_CONFIG = {
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
