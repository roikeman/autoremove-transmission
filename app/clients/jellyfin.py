import requests

ITEM_FIELDS = "Path,DateCreated,DateLastMediaAdded,MediaSources"


class JellyfinClient:
    def __init__(self, base_url, api_key, timeout=30):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["Authorization"] = f'MediaBrowser Token="{api_key}"'

    def _get(self, path, params=None):
        resp = self._session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def users(self):
        return self._get("/Users")

    def items(self, user_id, item_type):
        data = self._get(f"/Users/{user_id}/Items", {
            "IncludeItemTypes": item_type,
            "Recursive": "true",
            "Fields": ITEM_FIELDS,
        })
        return data.get("Items", [])

    def episodes(self, user_id, series_id):
        data = self._get(f"/Users/{user_id}/Items", {
            "ParentId": series_id,
            "IncludeItemTypes": "Episode",
            "Recursive": "true",
            "Fields": "DateCreated,MediaSources",
        })
        return data.get("Items", [])

    def played_episodes(self, user_id, limit=2000):
        """Every episode this user has played, newest-first, tagged with its
        owning SeriesId -- one call per user, not one call per (user,
        series) pair.

        Jellyfin populates UserData.LastPlayedDate on the Episode item, not
        on the Series item, so this is how a series' last-played date gets
        derived without the per-series fan-out (91 users x ~75 series) that
        was removed from _scan for performance. See app._scan / app._merge
        for how the per-user results here are folded into one series-level
        last-played date across all users.
        """
        data = self._get(f"/Users/{user_id}/Items", {
            "IncludeItemTypes": "Episode",
            "Recursive": "true",
            "Filters": "IsPlayed",
            "SortBy": "DatePlayed",
            "SortOrder": "Descending",
            "Fields": "SeriesId",
            "Limit": limit,
        })
        return data.get("Items", [])

    def delete_item(self, item_id):
        resp = self._session.delete(f"{self.base_url}/Items/{item_id}", timeout=self.timeout)
        if resp.status_code == 404:
            return  # already gone — idempotent
        resp.raise_for_status()

    def refresh_library(self):
        resp = self._session.post(f"{self.base_url}/Library/Refresh", timeout=self.timeout)
        resp.raise_for_status()
