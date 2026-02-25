"""Health monitor: notify via Discord and email when the app starts/restarts."""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText

import httpx

from ..config import settings

logger = logging.getLogger(__name__)


async def send_health_discord(message: str) -> None:
    """Send a message to the health monitoring Discord channel."""
    if not settings.health_webhook_url:
        return
    payload = {
        "embeds": [{
            "title": "ヤフアマ サーバー監視",
            "description": message,
            "color": 0xFF9800,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(settings.health_webhook_url, json=payload)
            resp.raise_for_status()
        logger.info("Health notification sent to Discord")
    except Exception as e:
        logger.warning("Failed to send health Discord notification: %s", e)


def send_health_email(subject: str, body: str) -> None:
    """Send a health alert email via Gmail SMTP."""
    if not (settings.alert_email_to and settings.alert_email_from and settings.alert_email_password):
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = settings.alert_email_from
    msg["To"] = settings.alert_email_to
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(settings.alert_email_from, settings.alert_email_password)
            server.send_message(msg)
        logger.info("Health alert email sent to %s", settings.alert_email_to)
    except Exception as e:
        logger.warning("Failed to send health alert email: %s", e)


async def notify_startup() -> None:
    """Notify that the app has started (or restarted after a crash)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    message = f"ヤフアマが起動しました（{now}）\nサーバーが再起動された可能性があります。"

    await send_health_discord(message)
    send_health_email(
        subject="[ヤフアマ] サーバー起動通知",
        body=message,
    )
