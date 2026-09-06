import requests

# Sonarr and Radarr share the v3 API shape but differ in resource name and in
# the spelling of the exclusion parameter.
KINDS = {
    "sonarr": {"resource": "series", "exclusion_param": "addImportListExclusion"},
    "radarr": {"resource": "movie",  "exclusion_param": "addImportExclusion"},
}


class ArrClient:
    def __init__(self, base_url, api_key, kind, timeout=60):
        if kind not in KINDS:
            raise ValueError(f"unknown arr kind: {kind}")
        self.kind = kind
        self.resource = KINDS[kind]["resource"]
        self.exclusion_param = KINDS[kind]["exclusion_param"]
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["X-Api-Key"] = api_key

    def list_items(self):
        resp = self._session.get(
            f"{self.base_url}/api/v3/{self.resource}", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def delete_item(self, item_id):
        """Delete the entry and its files. Never adds an import exclusion."""
        resp = self._session.delete(
            f"{self.base_url}/api/v3/{self.resource}/{item_id}",
            params={"deleteFiles": "true", self.exclusion_param: "false"},
            timeout=self.timeout,
        )
        if resp.status_code == 404:
            return  # already gone — idempotent
        resp.raise_for_status()
