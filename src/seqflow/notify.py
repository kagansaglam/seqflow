"""Reporting: structured logging plus pluggable notification channels."""
from __future__ import annotations

import json
import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

log = logging.getLogger("seqflow")


class Channel(Protocol):
    def send(self, level: str, subject: str, body: str) -> None: ...


class LogChannel:
    def send(self, level: str, subject: str, body: str) -> None:
        log.log(getattr(logging, level.upper(), logging.INFO),
                json.dumps({"subject": subject, "body": body}))


class EmailChannel:
    def __init__(self, host: str, sender: str, recipients: list[str], port: int = 25) -> None:
        self.host, self.port, self.sender, self.recipients = host, port, sender, recipients

    def send(self, level: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"] = self.sender, ", ".join(self.recipients)
        msg["Subject"] = f"[seqflow:{level}] {subject}"
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
            smtp.send_message(msg)


class Notifier:
    """Fans messages out to channels. Warnings go to log only; errors go everywhere."""

    def __init__(self, channels: list[Channel] | None = None, alert_channels: list[Channel] | None = None) -> None:
        self.channels = channels if channels is not None else [LogChannel()]
        self.alert_channels = alert_channels or []

    def notify(self, level: str, subject: str, body: str = "") -> None:
        for ch in self.channels:
            ch.send(level, subject, body)
        if level == "error":
            for ch in self.alert_channels:
                try:
                    ch.send(level, subject, body)
                except Exception:  # alerting must never crash the pipeline
                    log.exception("alert channel failed")
