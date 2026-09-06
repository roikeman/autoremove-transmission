import json
from cleanup import journal


def _redirect(tmp_path, monkeypatch):
    path = tmp_path / "cleanup-journal.jsonl"
    monkeypatch.setattr(journal, "JOURNAL_PATH", str(path))
    return path


def test_append_writes_one_line_per_entry(tmp_path, monkeypatch):
    path = _redirect(tmp_path, monkeypatch)
    journal.append({"title": "A", "status": "deleted"})
    journal.append({"title": "B", "status": "failed"})
    lines = path.read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["title"] == "A"


def test_append_adds_timestamp(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    journal.append({"title": "A"})
    assert "ts" in journal.read_recent()[0]


def test_read_recent_returns_newest_first(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    for name in ("A", "B", "C"):
        journal.append({"title": name})
    assert [e["title"] for e in journal.read_recent()] == ["C", "B", "A"]


def test_read_recent_respects_limit(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    for i in range(10):
        journal.append({"title": str(i)})
    assert len(journal.read_recent(limit=3)) == 3


def test_read_recent_on_missing_file(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    assert journal.read_recent() == []


def test_corrupt_line_is_skipped(tmp_path, monkeypatch):
    path = _redirect(tmp_path, monkeypatch)
    path.write_text('{"title": "good"}\nnot json\n')
    assert [e["title"] for e in journal.read_recent()] == ["good"]


def test_secrets_are_never_written(tmp_path, monkeypatch):
    path = _redirect(tmp_path, monkeypatch)
    journal.append({"title": "A", "jellyfin_api_key": "leak", "sonarr_api_key": "leak"})
    assert "leak" not in path.read_text()
