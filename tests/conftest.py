import os
import pytest


@pytest.fixture
def tmp_tree(tmp_path):
    """Build files with controlled hardlink counts.

    Returns a helper: make(name, size, linked=False) -> str path.
    A linked file gets a second hardlink, so st_nlink == 2.
    """
    links_dir = tmp_path / "links"
    files_dir = tmp_path / "files"
    links_dir.mkdir()
    files_dir.mkdir()

    def make(name, size=16, linked=False):
        path = files_dir / name
        path.write_bytes(b"x" * size)
        if linked:
            os.link(path, links_dir / name)
        return str(path)

    return make
