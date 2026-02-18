"""Log-based notifier – writes notifications to the application log."""

from __future__ import annotations

import logging

from ..models import MonitoredItem, StatusHistory
from .base import BaseNotifier

logger = logging.getLogger(__name__)


class LogNotifier(BaseNotifier):
    async def notify(self, item: MonitoredItem, change: StatusHistory) -> bool:
        msg = self.format_message(item, change)
        logger.info("NOTIFICATION:\n%s", msg)
        return True
