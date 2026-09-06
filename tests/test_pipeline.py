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
    def __init__(self, torrents=None, session_ratio_limit=None, session_ratio_limited=False):
        self.torrents = torrents or []
        self.removed = []
        self.session_ratio_limit = session_ratio_limit
        self.session_ratio_limited = session_ratio_limited

    def get_all_torrents(self):
        return self.torrents

    def remove_torrent(self, tid, delete_data=True):
        self.removed.append(tid)

    def is_deletable(self, torrent):
        return True

    def rpc_call(self, method, arguments):
        if method == "session-get":
            return {"arguments": {
                "seedRatioLimit": self.session_ratio_limit,
                "seedRatioLimited": self.session_ratio_limited,
            }}
        raise NotImplementedError(method)


class CountingTransmission(FakeTransmission):
    """Tracks how many times get_all_torrents() is called, to pin the sweep
    running once per execute() rather than once per title."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.get_all_calls = 0

    def get_all_torrents(self):
        self.get_all_calls += 1
        return super().get_all_torrents()


class RealIsDeletableTransmission(FakeTransmission):
    """Uses the real nlink-based is_deletable instead of hardcoding True --
    needed to prove step ordering, since a fake that always says "deletable"
    can't tell "owner delete already happened" from "not yet"."""

    def is_deletable(self, torrent):
        from clients.transmission import is_deletable as real_is_deletable
        return real_is_deletable(torrent)


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
    title_entries = [e for e in entries if "title" in e]
    assert title_entries[0]["title"] == "Show"


def test_execute_journals_each_destructive_step_immediately():
    # A hard process death between the owner delete and the Jellyfin delete
    # must still leave a record that the owner delete happened. Each
    # destructive step is journaled as it completes, not only at title
    # completion.
    pipeline.execute([_candidate()], CFG, _clients())
    entries = journal.read_recent()
    step_entries = [e for e in entries if e.get("kind") == "step"]

    assert any(e["step"].startswith("sonarr:delete/") and e["status"] == "ok"
               for e in step_entries)
    assert any(e["step"].startswith("jellyfin:delete/") and e["status"] == "ok"
               for e in step_entries)
    assert all(e["title"] == "Show" for e in step_entries)


def test_execute_step_journal_survives_jellyfin_failure():
    # Even when the Jellyfin step fails (title ends up "partial"), the
    # owner-delete step -- which already succeeded and already deleted
    # real data -- must have its own journal record.
    class Boom(FakeJellyfin):
        def delete_item(self, item_id):
            raise RuntimeError("jellyfin down")

    clients = _clients(jellyfin=Boom())
    pipeline.execute([_candidate()], CFG, clients)
    entries = journal.read_recent()
    step_entries = [e for e in entries if e.get("kind") == "step"]

    assert any(e["step"].startswith("sonarr:delete/") and e["status"] == "ok"
               for e in step_entries)
    assert not any(e["step"].startswith("jellyfin:delete/") for e in step_entries)


def test_title_completion_journal_entry_is_kind_title():
    pipeline.execute([_candidate()], CFG, _clients())
    entries = journal.read_recent()
    title_entries = [e for e in entries if e.get("kind") == "title"]
    assert title_entries[0]["title"] == "Show"
    assert title_entries[0]["status"] == "deleted"


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


def test_seed_guard_mode0_session_limit_disabled_sweeps():
    # Amended from test_seed_guard_ignores_torrent_with_no_limit: mode 0
    # means "use the session's global limit", not "use this torrent's own
    # (typically 0) seedRatioLimit field". With no session limit resolved
    # (disabled / unknown) there is no target, so the torrent may be swept.
    torrent = {"uploadRatio": 0.1, "seedRatioLimit": 0, "seedRatioMode": 0}
    assert pipeline.should_keep_seeding(torrent, session_ratio_limit=None) is False


def test_seed_guard_mode0_keeps_when_ratio_below_active_session_limit():
    torrent = {"uploadRatio": 0.5, "seedRatioLimit": 0, "seedRatioMode": 0}
    assert pipeline.should_keep_seeding(torrent, session_ratio_limit=2.0) is True


def test_seed_guard_mode0_sweeps_when_ratio_above_active_session_limit():
    torrent = {"uploadRatio": 3.0, "seedRatioLimit": 0, "seedRatioMode": 0}
    assert pipeline.should_keep_seeding(torrent, session_ratio_limit=2.0) is False


def test_seed_guard_mode2_always_keeps_regardless_of_limit():
    torrent = {"uploadRatio": 999.0, "seedRatioLimit": 1.0, "seedRatioMode": 2}
    assert pipeline.should_keep_seeding(torrent, session_ratio_limit=0.1) is True


def test_seed_guard_keeps_torrent_below_minimum_seed_time():
    # Ratio is already satisfied (mode 1, ratio 5.0 >= limit 1.0), but the
    # torrent has only been seeding 100s against a 3600s minimum -- must
    # still be kept.
    torrent = {"uploadRatio": 5.0, "seedRatioLimit": 1.0, "seedRatioMode": 1,
               "secondsSeeding": 100}
    assert pipeline.should_keep_seeding(torrent, min_seed_seconds=3600) is True


def test_seed_guard_releases_torrent_above_minimum_seed_time_and_ratio_met():
    torrent = {"uploadRatio": 5.0, "seedRatioLimit": 1.0, "seedRatioMode": 1,
               "secondsSeeding": 7200}
    assert pipeline.should_keep_seeding(torrent, min_seed_seconds=3600) is False


def test_seed_guard_minimum_seed_time_disabled_when_zero():
    # min_seed_seconds=0 (the falsy default) must not affect the outcome --
    # only the pre-existing ratio logic applies.
    torrent = {"uploadRatio": 5.0, "seedRatioLimit": 1.0, "seedRatioMode": 1,
               "secondsSeeding": 0}
    assert pipeline.should_keep_seeding(torrent, min_seed_seconds=0) is False


def test_execute_fetches_session_limit_once_for_mode0_seed_guard(tmp_path):
    lib_dir = tmp_path / "share" / "Show"
    lib_dir.mkdir(parents=True)
    lib_file = lib_dir / "ep.mkv"
    lib_file.write_bytes(b"x")

    dl_dir = tmp_path / "downloads" / "Show"
    dl_dir.mkdir(parents=True)
    os.link(str(lib_file), str(dl_dir / "ep.mkv"))

    torrent = {"id": 9, "downloadDir": str(dl_dir), "files": [{"name": "ep.mkv"}],
               "seedRatioMode": 0, "uploadRatio": 0.1}
    tm = FakeTransmission(torrents=[torrent], session_ratio_limit=2.0, session_ratio_limited=True)
    clients = _clients(transmission=tm)
    cfg = {**CFG, "library_roots": [str(tmp_path / "share")], "seed_guard": True}

    pipeline.execute([_candidate(owner=None, owner_id=None, path=str(lib_dir))], cfg, clients)

    # ratio 0.1 < session limit 2.0 -> kept, not removed.
    assert tm.removed == []


def test_execute_wires_min_seed_seconds_into_sweep(tmp_path):
    lib_dir = tmp_path / "share" / "Show"
    lib_dir.mkdir(parents=True)
    lib_file = lib_dir / "ep.mkv"
    lib_file.write_bytes(b"x")

    dl_dir = tmp_path / "downloads" / "Show"
    dl_dir.mkdir(parents=True)
    os.link(str(lib_file), str(dl_dir / "ep.mkv"))

    # Ratio is already satisfied (mode 1, ratio 5.0 >= limit 1.0) -- were
    # min_seed_seconds not wired through from cfg into the sweep, this
    # torrent would be removed.
    torrent = {"id": 9, "downloadDir": str(dl_dir), "files": [{"name": "ep.mkv"}],
               "seedRatioMode": 1, "seedRatioLimit": 1.0, "uploadRatio": 5.0,
               "secondsSeeding": 100}
    tm = FakeTransmission(torrents=[torrent])
    clients = _clients(transmission=tm)
    cfg = {**CFG, "library_roots": [str(tmp_path / "share")], "seed_guard": True,
           "min_seed_seconds": 3600}

    pipeline.execute([_candidate(owner=None, owner_id=None, path=str(lib_dir))], cfg, clients)

    assert tm.removed == []


def test_delete_tree_never_rmdirs_a_configured_root(tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    (root / "file.txt").write_text("x")

    freed = pipeline._delete_tree(str(root), [str(root)])

    assert freed == 1
    assert root.is_dir()  # the root itself must survive, only its file is gone
    assert not (root / "file.txt").exists()


def test_execute_raises_and_deletes_nothing_when_over_cap():
    clients = _clients()
    cands = [_candidate(jf_id=str(i), owner_id=i) for i in range(5)]
    cfg = {**CFG, "max_titles_per_run": 3}

    with pytest.raises(pipeline.BlastRadiusExceeded):
        pipeline.execute(cands, cfg, clients)

    assert clients.sonarr.deleted == []
    assert clients.jellyfin.deleted == []
    assert clients.transmission.removed == []


def test_sweep_runs_once_per_execute_not_per_title():
    tm = CountingTransmission()
    clients = _clients(transmission=tm)
    pipeline.execute(
        [_candidate(jf_id="a", owner_id=1), _candidate(jf_id="b", owner_id=2)],
        CFG, clients)
    assert tm.get_all_calls == 1


def test_title_status_does_not_depend_on_sweep_outcome():
    class BoomTransmission(FakeTransmission):
        def get_all_torrents(self):
            raise RuntimeError("transmission down")

    clients = _clients(transmission=BoomTransmission())
    results = pipeline.execute([_candidate()], CFG, clients)

    assert results[0]["status"] == "deleted"
    entries = journal.read_recent()
    sweep_entries = [e for e in entries if e.get("kind") == "transmission_sweep"]
    assert sweep_entries[0]["status"] == "error"


def test_sweep_only_removes_torrents_matching_a_deleted_titles_files(tmp_path):
    lib_dir_a = tmp_path / "share" / "ShowA"
    lib_dir_a.mkdir(parents=True)
    a_file = lib_dir_a / "a.mkv"
    a_file.write_bytes(b"a")

    dl_dir_a = tmp_path / "downloads" / "ShowA"
    dl_dir_a.mkdir(parents=True)
    os.link(str(a_file), str(dl_dir_a / "a.mkv"))

    unrelated_dir = tmp_path / "downloads" / "Unrelated"
    unrelated_dir.mkdir(parents=True)
    (unrelated_dir / "other.mkv").write_bytes(b"other")

    torrent_related = {"id": 1, "downloadDir": str(dl_dir_a), "files": [{"name": "a.mkv"}]}
    torrent_unrelated = {"id": 2, "downloadDir": str(unrelated_dir), "files": [{"name": "other.mkv"}]}

    tm = FakeTransmission(torrents=[torrent_related, torrent_unrelated])
    clients = _clients(transmission=tm)
    cfg = {**CFG, "library_roots": [str(tmp_path / "share")], "seed_guard": False}

    # owner="sonarr" here: the fake *arr doesn't touch disk, so this test
    # isolates the inode-matching/scoping logic from step ordering (that
    # is covered separately below).
    candidate = _candidate(jf_id="a1", owner="sonarr", owner_id=4, path=str(lib_dir_a))
    pipeline.execute([candidate], cfg, clients)

    assert tm.removed == [1]


def test_owner_deletion_runs_before_sweep_real_hardlinks(tmp_path):
    lib_dir = tmp_path / "share" / "Movie"
    lib_dir.mkdir(parents=True)
    lib_file = lib_dir / "movie.mkv"
    lib_file.write_bytes(b"data")

    dl_dir = tmp_path / "downloads" / "Movie"
    dl_dir.mkdir(parents=True)
    dl_file = dl_dir / "movie.mkv"
    os.link(str(lib_file), str(dl_file))
    assert os.stat(str(dl_file)).st_nlink == 2

    # seed_guard off keeps that guard out of the way so only ordering + real
    # hardlink state determine the outcome.
    torrent = {"id": 42, "downloadDir": str(dl_dir), "files": [{"name": "movie.mkv"}]}
    tm = RealIsDeletableTransmission(torrents=[torrent])
    clients = _clients(transmission=tm)
    cfg = {**CFG, "library_roots": [str(tmp_path / "share")], "seed_guard": False}

    candidate = _candidate(owner=None, owner_id=None, path=str(lib_dir))
    pipeline.execute([candidate], cfg, clients)

    # The owner/file-delete step removed the library hardlink first, so by
    # the time the sweep ran the download copy's nlink had already dropped
    # to 1 and it was correctly swept. Had the sweep run before the owner
    # step, nlink would still be 2, is_deletable() would return False, and
    # removed would be [] -- this assertion pins that ordering.
    assert os.stat(str(dl_file)).st_nlink == 1
    assert tm.removed == [42]
