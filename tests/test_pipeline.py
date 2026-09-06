import os
import pytest
from cleanup import pipeline, journal
from cleanup.candidates import Candidate
from datetime import datetime


@pytest.fixture(autouse=True)
def _journal_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_PATH", str(tmp_path / "j.jsonl"))


def _candidate(**over):
    base = dict(
        jf_id="s1", kind="series", title="Show", path="/share/series/Show",
        size_bytes=1000, added=datetime(2025, 1, 1), last_played=None,
        episodes=10, watched=0, progress_pct=0.0,
        owner="sonarr", owner_id=4, bucket="A", flags=[],
    )
    base.update(over)
    return Candidate(**base)


class FakeArr:
    def __init__(self):
        self.deleted = []

    def delete_item(self, item_id):
        self.deleted.append(item_id)


class FakeJellyfin:
    def __init__(self):
        self.deleted = []

    def delete_item(self, item_id):
        self.deleted.append(item_id)


class FakeTransmission:
    def __init__(self, torrents=None):
        self.torrents = torrents or []
        self.removed = []

    def get_all_torrents(self):
        return self.torrents

    def remove_torrent(self, tid, delete_data=True):
        self.removed.append(tid)

    def is_deletable(self, torrent):
        return True


def _clients(**over):
    c = pipeline.Clients(
        sonarr=FakeArr(), radarr=FakeArr(),
        jellyfin=FakeJellyfin(), transmission=FakeTransmission(),
    )
    for k, v in over.items():
        setattr(c, k, v)
    return c


CFG = {"library_roots": ["/share"], "seed_guard": True,
       "max_titles_per_run": 50, "max_bytes_per_run": 10 ** 12}


def test_plan_reports_totals_without_deleting():
    clients = _clients()
    result = pipeline.plan([_candidate()], CFG)
    assert result["count"] == 1
    assert result["titles"][0]["title"] == "Show"
    assert clients.sonarr.deleted == []


def test_plan_lists_steps_for_owned_title():
    steps = pipeline.plan([_candidate()], CFG)["titles"][0]["steps"]
    assert steps[0].startswith("sonarr:")
    assert any(s.startswith("jellyfin:") for s in steps)


def test_plan_skips_arr_step_for_unowned():
    steps = pipeline.plan([_candidate(owner=None, owner_id=None)], CFG)["titles"][0]["steps"]
    assert not any(s.startswith("sonarr:") for s in steps)
    assert any(s.startswith("files:") for s in steps)


def test_plan_raises_when_over_title_cap():
    cands = [_candidate(jf_id=str(i)) for i in range(5)]
    cfg = {**CFG, "max_titles_per_run": 3}
    with pytest.raises(pipeline.BlastRadiusExceeded):
        pipeline.plan(cands, cfg)


def test_plan_raises_when_over_byte_cap():
    cfg = {**CFG, "max_bytes_per_run": 500}
    with pytest.raises(pipeline.BlastRadiusExceeded):
        pipeline.plan([_candidate(size_bytes=1000)], cfg)


def test_execute_deletes_from_arr_then_jellyfin():
    clients = _clients()
    results = pipeline.execute([_candidate()], CFG, clients)
    assert clients.sonarr.deleted == [4]
    assert clients.jellyfin.deleted == ["s1"]
    assert results[0]["status"] == "deleted"


def test_execute_uses_radarr_for_movies():
    clients = _clients()
    pipeline.execute([_candidate(kind="movie", owner="radarr", owner_id=9)], CFG, clients)
    assert clients.radarr.deleted == [9]
    assert clients.sonarr.deleted == []


def test_execute_records_partial_on_jellyfin_failure():
    class Boom(FakeJellyfin):
        def delete_item(self, item_id):
            raise RuntimeError("jellyfin down")

    clients = _clients(jellyfin=Boom())
    results = pipeline.execute([_candidate()], CFG, clients)
    assert results[0]["status"] == "partial"
    assert clients.sonarr.deleted == [4]
    completed = [s["step"] for s in results[0]["steps"] if s["status"] == "ok"]
    assert any(s.startswith("sonarr") for s in completed)


def test_execute_records_failed_when_first_step_fails():
    class Boom(FakeArr):
        def delete_item(self, item_id):
            raise RuntimeError("sonarr down")

    clients = _clients(sonarr=Boom())
    results = pipeline.execute([_candidate()], CFG, clients)
    assert results[0]["status"] == "failed"
    assert clients.jellyfin.deleted == []


def test_execute_writes_journal_entry():
    pipeline.execute([_candidate()], CFG, _clients())
    entries = journal.read_recent()
    assert entries[0]["title"] == "Show"


def test_execute_continues_after_one_title_fails():
    class BoomOnce(FakeArr):
        def delete_item(self, item_id):
            if item_id == 1:
                raise RuntimeError("nope")
            self.deleted.append(item_id)

    clients = _clients(sonarr=BoomOnce())
    results = pipeline.execute(
        [_candidate(jf_id="a", owner_id=1), _candidate(jf_id="b", owner_id=2)],
        CFG, clients)
    assert results[0]["status"] == "failed"
    assert results[1]["status"] == "deleted"


def test_seed_guard_keeps_unsatisfied_torrent():
    torrent = {"uploadRatio": 0.4, "seedRatioLimit": 1.0, "seedRatioMode": 1}
    assert pipeline.should_keep_seeding(torrent) is True


def test_seed_guard_releases_satisfied_torrent():
    torrent = {"uploadRatio": 1.5, "seedRatioLimit": 1.0, "seedRatioMode": 1}
    assert pipeline.should_keep_seeding(torrent) is False


def test_seed_guard_ignores_torrent_with_no_limit():
    torrent = {"uploadRatio": 0.1, "seedRatioLimit": 0, "seedRatioMode": 0}
    assert pipeline.should_keep_seeding(torrent) is False
