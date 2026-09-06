import os
import pytest
from cleanup.paths import assert_within_roots, delete_file, PathOutsideRoots


def test_accepts_path_inside_root(tmp_path):
    root = str(tmp_path)
    target = os.path.join(root, "a", "b.mkv")
    assert assert_within_roots(target, [root]) == os.path.normpath(target)


def test_rejects_path_outside_root(tmp_path):
    with pytest.raises(PathOutsideRoots):
        assert_within_roots("/etc/passwd", [str(tmp_path)])


def test_rejects_traversal(tmp_path):
    root = str(tmp_path / "share")
    with pytest.raises(PathOutsideRoots):
        assert_within_roots(os.path.join(root, "..", "..", "etc", "passwd"), [root])


def test_rejects_sibling_prefix(tmp_path):
    root = str(tmp_path / "share")
    with pytest.raises(PathOutsideRoots):
        assert_within_roots(str(tmp_path / "shared" / "x.mkv"), [root])


def test_rejects_relative_path(tmp_path):
    with pytest.raises(PathOutsideRoots):
        assert_within_roots("relative/path.mkv", [str(tmp_path)])


def test_rejects_empty_roots(tmp_path):
    with pytest.raises(PathOutsideRoots):
        assert_within_roots(str(tmp_path / "x.mkv"), [])


def test_rejects_symlink_escaping_root(tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"x" * 8)
    link = root / "link.mkv"
    os.symlink(outside, link)
    with pytest.raises(PathOutsideRoots):
        assert_within_roots(str(link), [str(root)])


def test_delete_file_removes_and_reports_bytes(tmp_path):
    root = tmp_path
    target = root / "a.mkv"
    target.write_bytes(b"x" * 100)
    assert delete_file(str(target), [str(root)]) == 100
    assert not target.exists()


def test_delete_file_missing_is_zero_not_error(tmp_path):
    assert delete_file(str(tmp_path / "ghost.mkv"), [str(tmp_path)]) == 0


def test_delete_file_refuses_outside_root(tmp_path):
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"x")
    root = tmp_path / "share"
    root.mkdir()
    with pytest.raises(PathOutsideRoots):
        delete_file(str(outside), [str(root)])
    assert outside.exists()


def test_delete_file_prunes_empty_parent(tmp_path):
    nested = tmp_path / "show" / "season"
    nested.mkdir(parents=True)
    target = nested / "ep.mkv"
    target.write_bytes(b"x")
    delete_file(str(target), [str(tmp_path)])
    assert not nested.exists()


def test_delete_file_does_not_remove_root_itself(tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    target = root / "only.mkv"
    target.write_bytes(b"x")
    delete_file(str(target), [str(root)])
    assert not target.exists()
    assert root.exists()


def test_empty_string_root_rejects_path_under_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "x.mkv"
    target.write_bytes(b"x")
    with pytest.raises(PathOutsideRoots):
        assert_within_roots(str(target), [""])
