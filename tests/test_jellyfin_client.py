import pytest
import responses
from clients.jellyfin import JellyfinClient

BASE = "http://jf.local:8096"


@pytest.fixture
def client():
    return JellyfinClient(BASE, "test-key")


@responses.activate
def test_auth_header_uses_mediabrowser_token(client):
    responses.add(responses.GET, f"{BASE}/Users", json=[], status=200)
    client.users()
    assert 'MediaBrowser Token="test-key"' in responses.calls[0].request.headers["Authorization"]


@responses.activate
def test_items_requests_required_fields(client):
    responses.add(responses.GET, f"{BASE}/Users/u1/Items",
                  json={"Items": [{"Id": "x"}]}, status=200)
    items = client.items("u1", "Series")
    assert items == [{"Id": "x"}]
    qs = responses.calls[0].request.url
    assert "IncludeItemTypes=Series" in qs
    assert "Recursive=true" in qs
    assert "DateLastMediaAdded" in qs


@responses.activate
def test_items_returns_empty_list_when_absent(client):
    responses.add(responses.GET, f"{BASE}/Users/u1/Items", json={}, status=200)
    assert client.items("u1", "Movie") == []


@responses.activate
def test_episodes_scopes_to_parent(client):
    responses.add(responses.GET, f"{BASE}/Users/u1/Items",
                  json={"Items": []}, status=200)
    client.episodes("u1", "series-9")
    assert "ParentId=series-9" in responses.calls[0].request.url
    assert "IncludeItemTypes=Episode" in responses.calls[0].request.url


@responses.activate
def test_delete_item_issues_delete(client):
    responses.add(responses.DELETE, f"{BASE}/Items/abc", status=204)
    client.delete_item("abc")
    assert responses.calls[0].request.method == "DELETE"


@responses.activate
def test_delete_item_treats_404_as_success(client):
    responses.add(responses.DELETE, f"{BASE}/Items/gone", status=404)
    client.delete_item("gone")  # must not raise


@responses.activate
def test_delete_item_raises_on_server_error(client):
    responses.add(responses.DELETE, f"{BASE}/Items/x", status=500)
    with pytest.raises(Exception):
        client.delete_item("x")


@responses.activate
def test_refresh_library_posts(client):
    responses.add(responses.POST, f"{BASE}/Library/Refresh", status=204)
    client.refresh_library()
    assert responses.calls[0].request.method == "POST"


def test_base_url_trailing_slash_is_normalized():
    assert JellyfinClient(BASE + "/", "k").base_url == BASE


@responses.activate
def test_played_episodes_requests_one_call_with_expected_params(client):
    responses.add(responses.GET, f"{BASE}/Users/u1/Items",
                  json={"Items": [{"Id": "e1", "SeriesId": "s1"}]}, status=200)
    items = client.played_episodes("u1")
    assert items == [{"Id": "e1", "SeriesId": "s1"}]
    qs = responses.calls[0].request.url
    assert "IncludeItemTypes=Episode" in qs
    assert "Recursive=true" in qs
    assert "Filters=IsPlayed" in qs
    assert "SortBy=DatePlayed" in qs
    assert "SortOrder=Descending" in qs
    assert "SeriesId" in qs
    assert "Limit=2000" in qs
    assert len(responses.calls) == 1


@responses.activate
def test_played_episodes_returns_empty_list_when_absent(client):
    responses.add(responses.GET, f"{BASE}/Users/u1/Items", json={}, status=200)
    assert client.played_episodes("u1") == []


@responses.activate
def test_played_episodes_respects_custom_limit(client):
    responses.add(responses.GET, f"{BASE}/Users/u1/Items", json={"Items": []}, status=200)
    client.played_episodes("u1", limit=50)
    assert "Limit=50" in responses.calls[0].request.url
