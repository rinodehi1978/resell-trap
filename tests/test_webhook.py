"""Tests for webhook notifier (Discord, Slack, LINE Notify)."""

from unittest.mock import AsyncMock, patch

import pytest

from yafuama.notifier.webhook import LINE_NOTIFY_URL, WebhookNotifier, send_webhook


class TestSendWebhook:
    @pytest.mark.asyncio
    async def test_json_post_discord(self):
        with patch("yafuama.notifier.webhook.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_resp = AsyncMock()
            mock_resp.raise_for_status = lambda: None
            mock_client.post.return_value = mock_resp

            result = await send_webhook(
                "https://discord.com/api/webhooks/xxx",
                {"content": "test"},
                webhook_type="discord",
            )
            assert result is True
            mock_client.post.assert_called_once()
            # Discord sends JSON
            call_kwargs = mock_client.post.call_args
            assert call_kwargs.kwargs.get("json") == {"content": "test"}

    @pytest.mark.asyncio
    async def test_form_post_line(self):
        with patch("yafuama.notifier.webhook.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_resp = AsyncMock()
            mock_resp.raise_for_status = lambda: None
            mock_client.post.return_value = mock_resp

            result = await send_webhook(
                LINE_NOTIFY_URL,
                {"message": "test msg", "token": "my-token"},
                webhook_type="line",
            )
            assert result is True
            call_kwargs = mock_client.post.call_args
            # LINE sends form data, not JSON
            assert call_kwargs.kwargs.get("data") == {"message": "test msg"}
            assert "Bearer my-token" in call_kwargs.kwargs.get("headers", {}).get("Authorization", "")

    @pytest.mark.asyncio
    async def test_retry_on_failure(self):
        with patch("yafuama.notifier.webhook.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post.side_effect = Exception("connection error")

            with patch("yafuama.notifier.webhook.asyncio.sleep", new_callable=AsyncMock):
                result = await send_webhook(
                    "https://example.com/webhook",
                    {"msg": "test"},
                    webhook_type="discord",
                    max_retries=3,
                )
            assert result is False
            assert mock_client.post.call_count == 3


class TestWebhookNotifierPayload:
    def test_discord_payload(self):
        from yafuama.models import MonitoredItem
        notifier = WebhookNotifier(url="https://discord.com/xxx", webhook_type="discord")
        item = MonitoredItem(
            auction_id="test123", title="Test Item", url="https://example.com",
            current_price=5000, bid_count=3, status="active", image_url="",
        )
        payload = notifier._build_payload("test message", item)
        assert "embeds" in payload
        assert payload["embeds"][0]["title"] == "Test Item"

    def test_slack_payload(self):
        from yafuama.models import MonitoredItem
        notifier = WebhookNotifier(url="https://hooks.slack.com/xxx", webhook_type="slack")
        item = MonitoredItem(
            auction_id="test123", title="Test Item", url="https://example.com",
            current_price=5000, bid_count=3, status="active", image_url="",
        )
        payload = notifier._build_payload("test message", item)
        assert "text" in payload
        assert "blocks" in payload

    def test_line_payload(self):
        from yafuama.models import MonitoredItem
        notifier = WebhookNotifier(url="my-line-token", webhook_type="line")
        item = MonitoredItem(
            auction_id="test123", title="Test Item", url="https://example.com",
            current_price=5000, bid_count=3, status="active", image_url="",
        )
        payload = notifier._build_payload("test message", item)
        assert payload["message"] == "test message"
        assert payload["token"] == "my-line-token"
