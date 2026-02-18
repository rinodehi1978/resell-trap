"""Tests for ORM models."""

from datetime import datetime

from resell_trap.models import MonitoredItem, NotificationLog, StatusHistory


class TestMonitoredItem:
    def test_create_item(self, db):
        item = MonitoredItem(
            auction_id="123456789",
            title="Test Item",
            url="https://auctions.yahoo.co.jp/jp/auction/123456789",
            current_price=1000,
            status="active",
        )
        db.add(item)
        db.commit()

        loaded = db.query(MonitoredItem).filter_by(auction_id="123456789").one()
        assert loaded.title == "Test Item"
        assert loaded.current_price == 1000
        assert loaded.status == "active"
        assert loaded.is_monitoring_active is True

    def test_unique_auction_id(self, db):
        item1 = MonitoredItem(auction_id="111", title="A")
        item2 = MonitoredItem(auction_id="111", title="B")
        db.add(item1)
        db.commit()
        db.add(item2)
        try:
            db.commit()
            assert False, "Should have raised IntegrityError"
        except Exception:
            db.rollback()


class TestStatusHistory:
    def test_create_history(self, db):
        item = MonitoredItem(auction_id="999", title="T")
        db.add(item)
        db.flush()

        history = StatusHistory(
            item_id=item.id,
            auction_id="999",
            change_type="status_change",
            old_status="active",
            new_status="ended_sold",
        )
        db.add(history)
        db.commit()

        records = db.query(StatusHistory).filter_by(item_id=item.id).all()
        assert len(records) == 1
        assert records[0].change_type == "status_change"


class TestNotificationLog:
    def test_create_log(self, db):
        item = MonitoredItem(auction_id="888", title="T")
        db.add(item)
        db.flush()

        log = NotificationLog(
            item_id=item.id,
            channel="log",
            event_type="ended",
            message="test",
            success=True,
        )
        db.add(log)
        db.commit()

        logs = db.query(NotificationLog).filter_by(item_id=item.id).all()
        assert len(logs) == 1
        assert logs[0].success is True
