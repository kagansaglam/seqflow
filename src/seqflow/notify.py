"""Reporting: structured logging plus pluggable notification channels.

Channels are built from environment variables (notifier_from_env) so that secrets such
as the Slack webhook URL or the SMTP password never appear in code, command lines or
the database:

    SEQFLOW_SLACK_WEBHOOK     incoming-webhook URL
    SEQFLOW_SMTP_HOST         enables e-mail; also needs SEQFLOW_MAIL_TO
    SEQFLOW_SMTP_PORT         default 25
    SEQFLOW_SMTP_STARTTLS     "1" to upgrade the connection with STARTTLS
    SEQFLOW_SMTP_USER / SEQFLOW_SMTP_PASSWORD
    SEQFLOW_MAIL_FROM         default seqflow@<hostname>
    SEQFLOW_MAIL_TO           comma-separated recipients
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from email.message import EmailMessage
from typing import Protocol

from .exceptions import NotificationError
from .retry import retry_call

log = logging.getLogger("seqflow")


class Channel(Protocol):
    def send(self, level: str, subject: str, body: str) -> None: ...


class LogChannel:
    def send(self, level: str, subject: str, body: str) -> None:
        log.log(getattr(logging, level.upper(), logging.INFO),
                json.dumps({"subject": subject, "body": body}))


class EmailChannel:
    def __init__(self, host: str, sender: str, recipients: list[str], port: int = 25, *,
                 starttls: bool = False, username: str | None = None,
                 password: str | None = None, attempts: int = 3,
                 smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.host, self.port, self.sender, self.recipients = host, port, sender, recipients
        self.starttls, self.username, self.password = starttls, username, password
        self.attempts, self._smtp_factory, self._sleep = attempts, smtp_factory, sleep

    def __repr__(self) -> str:
        return f"EmailChannel(host={self.host!r}, port={self.port}, to={self.recipients!r})"

    def send(self, level: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"] = self.sender, ", ".join(self.recipients)
        msg["Subject"] = f"[seqflow:{level}] {subject}"
        msg.set_content(body)

        def attempt() -> None:
            with self._smtp_factory(self.host, self.port, timeout=15) as smtp:
                if self.starttls:
                    smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password or "")
                smtp.send_message(msg)

        retry_call(attempt, attempts=self.attempts, retry_on=(OSError,), sleep=self._sleep)


class SlackChannel:
    """Posts to a Slack incoming webhook. Network errors and HTTP 5xx are retried;
    HTTP 4xx (revoked or wrong webhook) is not, because retrying cannot help."""

    def __init__(self, webhook_url: str, *, timeout: float = 10.0, attempts: int = 3,
                 opener: Callable[..., object] = urllib.request.urlopen,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._url = webhook_url
        self.timeout, self.attempts = timeout, attempts
        self._open, self._sleep = opener, sleep

    def __repr__(self) -> str:  # the URL is a credential: never print it
        return "SlackChannel(webhook=***)"

    def send(self, level: str, subject: str, body: str) -> None:
        text = f"*[{level.upper()}] {subject}*"
        if body:
            text += f"\n{body}"
        data = json.dumps({"text": text}).encode("utf-8")

        def attempt() -> None:
            request = urllib.request.Request(
                self._url, data=data, method="POST",
                headers={"Content-Type": "application/json"})
            try:
                with self._open(request, timeout=self.timeout) as resp:
                    resp.read()
            except urllib.error.HTTPError as exc:
                if 400 <= exc.code < 500:
                    raise NotificationError(f"Slack rejected the message (HTTP {exc.code})") from None
                raise

        retry_call(attempt, attempts=self.attempts, retry_on=(OSError,), sleep=self._sleep)


class Notifier:
    """Fans messages out to channels. Warnings go to the log only; errors also go to
    the alert channels (Slack, e-mail)."""

    def __init__(self, channels: list[Channel] | None = None, alert_channels: list[Channel] | None = None) -> None:
        self.channels = channels if channels is not None else [LogChannel()]
        self.alert_channels = alert_channels or []

    def notify(self, level: str, subject: str, body: str = "") -> None:
        for ch in self.channels:
            ch.send(level, subject, body)
        if level == "error":
            self._send_alerts(level, subject, body)

    def notify_all(self, level: str, subject: str, body: str = "") -> None:
        """Send to every channel regardless of level (used for scheduled digests)."""
        for ch in self.channels:
            ch.send(level, subject, body)
        self._send_alerts(level, subject, body)

    def _send_alerts(self, level: str, subject: str, body: str) -> None:
        for ch in self.alert_channels:
            try:
                ch.send(level, subject, body)
            except Exception:  # alerting must never crash the pipeline
                log.exception("alert channel %r failed", ch)


def notifier_from_env(env: Mapping[str, str] | None = None) -> Notifier:
    env = os.environ if env is None else env
    alerts: list[Channel] = []
    if webhook := env.get("SEQFLOW_SLACK_WEBHOOK"):
        alerts.append(SlackChannel(webhook))
    host, to = env.get("SEQFLOW_SMTP_HOST"), env.get("SEQFLOW_MAIL_TO")
    if host and to:
        alerts.append(EmailChannel(
            host=host,
            port=int(env.get("SEQFLOW_SMTP_PORT", "25")),
            sender=env.get("SEQFLOW_MAIL_FROM") or f"seqflow@{socket.gethostname()}",
            recipients=[r.strip() for r in to.split(",") if r.strip()],
            starttls=env.get("SEQFLOW_SMTP_STARTTLS") == "1",
            username=env.get("SEQFLOW_SMTP_USER"),
            password=env.get("SEQFLOW_SMTP_PASSWORD"),
        ))
    elif host or to:
        log.warning("e-mail alerts need both SEQFLOW_SMTP_HOST and SEQFLOW_MAIL_TO; e-mail disabled")
    return Notifier(alert_channels=alerts)
