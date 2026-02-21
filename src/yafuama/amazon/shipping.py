"""Shipping pattern definitions for Yahoo→Amazon listing flow."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import settings


@dataclass(frozen=True)
class ShippingPattern:
    key: str  # "1_2_days", "2_3_days", "3_7_days"
    label: str  # UI表示用ラベル
    lead_time_days: int
    template_name: str  # Amazon配送テンプレート名


@dataclass(frozen=True)
class DeliveryRegion:
    region: str
    areas: str
    days: str


DELIVERY_REGIONS = [
    DeliveryRegion("関東", "千葉・東京・神奈川・埼玉・群馬・茨城・栃木・山梨", "1〜2日"),
    DeliveryRegion("本州その他", "東北・中部・関西・中国", "1〜2日"),
    DeliveryRegion("四国", "", "1〜2日"),
    DeliveryRegion("九州", "", "2〜3日"),
    DeliveryRegion("北海道", "", "2〜3日"),
    DeliveryRegion("沖縄・離島", "", "3〜4日"),
]

VALID_SHIPPING_PATTERN_KEYS = ("1_2_days", "2_3_days", "3_7_days")


def get_shipping_patterns() -> list[ShippingPattern]:
    tid = settings.shipping_template_id
    return [
        ShippingPattern("1_2_days", "1〜2日で発送", 4, tid),
        ShippingPattern("2_3_days", "2〜3日で発送", 6, tid),
        ShippingPattern("3_7_days", "3〜7日で発送", 9, tid),
    ]


def get_pattern_by_key(key: str) -> ShippingPattern | None:
    for p in get_shipping_patterns():
        if p.key == key:
            return p
    return None
