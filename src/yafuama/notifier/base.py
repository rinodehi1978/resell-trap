"""Notification interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import MonitoredItem, StatusHistory


class BaseNotifier(ABC):
    """Abstract base for notification channels."""

    @abstractmethod
    async def notify(self, item: MonitoredItem, change: StatusHistory) -> bool:
        """Send a notification. Return True on success."""
        ...

    def format_message(self, item: MonitoredItem, change: StatusHistory) -> str:
        lines = [f"[{change.change_type}] {item.title}"]
        lines.append(f"Auction: {item.auction_id}")

        if change.change_type == "status_change":
            lines.append(f"Status: {change.old_status} → {change.new_status}")
        elif change.change_type == "price_change":
            lines.append(f"Price: ¥{change.old_price or 0:,} → ¥{change.new_price or 0:,}")
        elif change.change_type == "bid_change":
            lines.append(f"Bids: {change.old_bid_count or 0} → {change.new_bid_count or 0}")

        lines.append(f"URL: {item.url}")
        return "\n".join(lines)
