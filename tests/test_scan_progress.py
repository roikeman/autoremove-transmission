"""Fix 2: scan progress must not go backwards.

_scan() makes TWO per-user passes over Jellyfin (the played-episodes query,
then the items fetch) -- production saw the reported counter restart at a
low number for the second pass (68/91 then 8/91), which reads as a fault
even though the scan is fine. progress_cb must now report a single
monotonic counter (done, total, phase) over the WHOLE scan -- total
accounts for both per-user passes plus the other phases -- so the number
only ever increases.
"""
import pytest


class _FakeJellyfin:
    users_list = []

    def __init__(self, base_url, api_key):
        pass

    @classmethod
    def reset(cls, users_list):
        cls.users_list = users_list

    def users(self):
        return list(self.users_list)

    def played_episodes(self, user_id, limit=2000):
        return []

    def items(self, user_id, item_type):
        return []


class _FakeArr:
    def __init__(self, base_url, api_key, kind):
        pass

    def list_items(self):
        return []


def _patch(monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "JellyfinClient", _FakeJellyfin)
    monkeypatch.setattr(app_module, "ArrClient", _FakeArr)
    return app_module


def _cfg():
    return {
        "jellyfin_url": "https://jf", "jellyfin_api_key": "key",
        "age_days": 180, "idle_days": 90,
    }


def test_progress_is_monotonic_across_both_user_passes(monkeypatch):
    app_module = _patch(monkeypatch)
    users = [{"Id": f"u{i}"} for i in range(6)]
    _FakeJellyfin.reset(users)

    events = []
    app_module._scan(_cfg(), progress_cb=lambda done, total, phase: events.append((done, total, phase)))

    assert events
    seen_done = [e[0] for e in events]
    for prev, cur in zip(seen_done, seen_done[1:]):
        assert cur >= prev, f"progress went backwards: {seen_done}"


def test_progress_total_accounts_for_both_user_passes(monkeypatch):
    app_module = _patch(monkeypatch)
    users = [{"Id": f"u{i}"} for i in range(6)]
    _FakeJellyfin.reset(users)

    events = []
    app_module._scan(_cfg(), progress_cb=lambda done, total, phase: events.append((done, total, phase)))

    totals = {e[1] for e in events}
    assert len(totals) == 1, "total must stay fixed across the whole scan"
    total = totals.pop()
    assert total >= 2 * len(users)

    # The final report must reach the declared total -- not stop short of it.
    assert events[-1][0] == total


def test_progress_reports_distinct_phases_for_each_pass(monkeypatch):
    app_module = _patch(monkeypatch)
    users = [{"Id": f"u{i}"} for i in range(3)]
    _FakeJellyfin.reset(users)

    events = []
    app_module._scan(_cfg(), progress_cb=lambda done, total, phase: events.append((done, total, phase)))

    phases = {e[2] for e in events}
    # A human-readable label naming what's happening now, not a generic
    # "jellyfin" reused (unchanged) for both passes.
    assert len(phases) >= 3
