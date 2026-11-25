"""The one client to the backend, with the triage agent's key."""

import httpx2

TRIES = 3


class Connection:
    def __init__(self, url: str, key: str):
        self._http = httpx2.Client(
            base_url=url,
            headers={"X-Triage-Agent-Key": key},
            timeout=30,
            trust_env=False,
        )

    def close(self) -> None:
        self._http.close()

    def send(self, method: str, path: str, body: dict | None = None) -> httpx2.Response:
        # A move is idempotent, so one whose answer was lost is sent again as it was.
        tried = 1
        while True:
            try:
                return self._http.request(method, path, json=body)
            except httpx2.TransportError:
                if tried == TRIES:
                    raise
                tried += 1
