"""Webhook notifier for Discord / Slack / LINE."""

from __future__ import annotations

import logging

import httpx

from ..config import settings
from ..models import MonitoredItem, StatusHistory
from .base import BaseNotifier

logger = logging.getLogger(__name__)


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

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.url, json=payload)
                resp.raise_for_status()
            return True
        except Exception as e:
            logger.warning("Webhook send failed: %s", e)
            return False

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
        else:
            # Generic / LINE
            return {"message": message}
