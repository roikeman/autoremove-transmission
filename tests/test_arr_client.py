import pytest
import responses
from clients.arr import ArrClient

BASE = "http://arr.local:8989"


@responses.activate
def test_sonarr_lists_series():
    responses.add(responses.GET, f"{BASE}/api/v3/series",
                  json=[{"id": 1, "path": "/share/series/Show"}], status=200)
    items = ArrClient(BASE, "k", "sonarr").list_items()
    assert items[0]["path"] == "/share/series/Show"


@responses.activate
def test_radarr_lists_movies():
    responses.add(responses.GET, f"{BASE}/api/v3/movie",
                  json=[{"id": 7, "path": "/share/movies/Film"}], status=200)
    items = ArrClient(BASE, "k", "radarr").list_items()
    assert items[0]["id"] == 7


@responses.activate
def test_api_key_sent_as_header():
    responses.add(responses.GET, f"{BASE}/api/v3/series", json=[], status=200)
    ArrClient(BASE, "secret-key", "sonarr").list_items()
    assert responses.calls[0].request.headers["X-Api-Key"] == "secret-key"


@responses.activate
def test_sonarr_delete_sends_correct_params():
    responses.add(responses.DELETE, f"{BASE}/api/v3/series/5", status=200)
    ArrClient(BASE, "k", "sonarr").delete_item(5)
    url = responses.calls[0].request.url
    assert "deleteFiles=true" in url
    assert "addImportListExclusion=false" in url


@responses.activate
def test_radarr_delete_sends_correct_params():
    responses.add(responses.DELETE, f"{BASE}/api/v3/movie/9", status=200)
    ArrClient(BASE, "k", "radarr").delete_item(9)
    url = responses.calls[0].request.url
    assert "deleteFiles=true" in url
    assert "addImportExclusion=false" in url


@responses.activate
def test_delete_treats_404_as_success():
    responses.add(responses.DELETE, f"{BASE}/api/v3/series/404", status=404)
    ArrClient(BASE, "k", "sonarr").delete_item(404)  # must not raise


@responses.activate
def test_delete_raises_on_server_error():
    responses.add(responses.DELETE, f"{BASE}/api/v3/series/1", status=500)
    with pytest.raises(Exception):
        ArrClient(BASE, "k", "sonarr").delete_item(1)


def test_unknown_kind_rejected():
    with pytest.raises(ValueError):
        ArrClient(BASE, "k", "lidarr")
