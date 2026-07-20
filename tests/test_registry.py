import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from registry import SnapshotCache, SubscriptionRegistry


def test_subscription_registry_deduplicates_symbols():
    registry = SubscriptionRegistry()
    assert registry.subscribe("c", "s", ["au", "au"]) == ["au"]
    assert registry.active_symbols() == ["au"]
    assert registry.unsubscribe("c", "s", ["au"]) == ["au"]


def test_snapshot_cache_can_peek_without_age_check():
    cache = SnapshotCache()
    cache.put("account", {"balance": 1})
    assert cache.get("account", 1) == {"balance": 1}
    assert cache.peek("account") == {"balance": 1}

