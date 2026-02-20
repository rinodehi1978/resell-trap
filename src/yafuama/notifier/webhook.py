"""Webhook notifier for Discord / Slack / LINE."""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import settings
from ..models import MonitoredItem, StatusHistory
from .base import BaseNotifier

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF = (1, 3, 5)  # seconds
LINE_NOTIFY_URL = "https://notify-api.line.me/api/notify"


async def send_webhook(
    url: str,
    payload: dict,
    *,
    webhook_type: str = "discord",
    max_retries: int = MAX_RETRIES,
) -> bool:
    """POST to a webhook URL with retry + exponential backoff.

    For LINE Notify, sends form-encoded data with Bearer token auth.
    For Discord/Slack/generic, sends JSON.
    """
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                if webhook_type == "line":
                    # LINE Notify: form-encoded with Bearer token in URL or separate
                    token = payload.get("token", "")
                    resp = await client.post(
                        url,
                        data={"message": payload["message"]},
                        headers={"Authorization": f"Bearer {token}"} if token else {},
                    )
                else:
                    resp = await client.post(url, json=payload)
                resp.raise_for_status()
            return True
        except Exception as e:
            wait = RETRY_BACKOFF[attempt] if attempt < len(RETRY_BACKOFF) else RETRY_BACKOFF[-1]
            if attempt < max_retries - 1:
                logger.warning("Webhook attempt %d/%d failed: %s (retry in %ds)", attempt + 1, max_retries, e, wait)
                await asyncio.sleep(wait)
            else:
                logger.warning("Webhook failed after %d attempts: %s", max_retries, e)
    return False


class WebhookNotifier(BaseNotifier):
    def __init__(self, url: str | None = None, webhook_type: str | None = None) -> None:
        self.url = url or settings.webhook_url
        self.webhook_type = webhook_type or settings.webhook_type

    async def notify(self, item: MonitoredItem, change: StatusHistory) -> bool:
        if not self.url:
            logger.debug("Webhook URL not configured; skipping")
            return False

        msg = self.format_message(item, change)
        payload = self._build_payload(msg, item)
        url = LINE_NOTIFY_URL if self.webhook_type == "line" else self.url
        return await send_webhook(url, payload, webhook_type=self.webhook_type)

    def _build_payload(self, message: str, item: MonitoredItem) -> dict:
        if self.webhook_type == "discord":
            return {
                "content": message,
                "embeds": [{
                    "title": item.title,
                    "url": item.url,
                    "color": 0xFF4500 if item.status.startswith("ended") else 0x00BFFF,
                    "fields": [
                        {"name": "Price", "value": f"¥{item.current_price:,}", "inline": True},
                        {"name": "Bids", "value": str(item.bid_count), "inline": True},
                        {"name": "Status", "value": item.status, "inline": True},
                    ],
                    "thumbnail": {"url": item.image_url} if item.image_url else {},
                }],
            }
        elif self.webhook_type == "slack":
            return {
                "text": message,
                "blocks": [{
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": message},
                }],
            }
        elif self.webhook_type == "line":
            # LINE Notify: token is stored in self.url, message sent as form data
            return {"message": message, "token": self.url}
        else:
            # Generic
            return {"message": message}
