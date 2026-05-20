"""Cliente fino para a HTTP API do Grafana.

Apenas endpoints usados por backup/restore. Documentação oficial:
https://grafana.com/docs/grafana/latest/developers/http_api/
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterable
from urllib.parse import urljoin

import requests

log = logging.getLogger(__name__)


class GrafanaError(RuntimeError):
    pass


class GrafanaClient:
    def __init__(self, base_url: str, token: str, timeout: int = 30):
        if not base_url:
            raise GrafanaError("base_url vazio")
        if not token:
            raise GrafanaError("token vazio")
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    # ---- núcleo HTTP -------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = urljoin(self.base_url, path.lstrip("/"))
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("Falha de rede (%s %s) tentativa %d: %s", method, path, attempt, exc)
                time.sleep(2 * attempt)
                continue

            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                log.warning("HTTP %s em %s (tentativa %d): %s", resp.status_code, path, attempt, resp.text[:200])
                time.sleep(2 * attempt)
                continue

            if not resp.ok:
                raise GrafanaError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")

            if not resp.content:
                return None
            return resp.json()

        raise GrafanaError(f"{method} {path} falhou após retries: {last_exc}")

    # ---- saúde -------------------------------------------------------------
    def health(self) -> dict:
        return self._request("GET", "/api/health")

    # ---- pastas ------------------------------------------------------------
    def list_folders(self) -> list[dict]:
        # limit alto para evitar paginação; Grafana suporta até 5000
        return self._request("GET", "/api/folders", params={"limit": 5000}) or []

    def get_folder_by_uid(self, uid: str) -> dict:
        return self._request("GET", f"/api/folders/{uid}")

    def create_folder(self, uid: str, title: str) -> dict:
        return self._request("POST", "/api/folders", json={"uid": uid, "title": title})

    # ---- dashboards --------------------------------------------------------
    def search_dashboards(self) -> list[dict]:
        """Lista todos os dashboards (type=dash-db), paginando."""
        out: list[dict] = []
        page = 1
        while True:
            batch = self._request(
                "GET",
                "/api/search",
                params={"type": "dash-db", "limit": 1000, "page": page},
            ) or []
            out.extend(batch)
            if len(batch) < 1000:
                break
            page += 1
        return out

    def get_dashboard(self, uid: str) -> dict:
        """Retorna payload completo: {dashboard, meta}."""
        return self._request("GET", f"/api/dashboards/uid/{uid}")

    def import_dashboard(self, dashboard: dict, folder_uid: str | None, message: str) -> dict:
        payload = {
            "dashboard": dashboard,
            "overwrite": True,
            "message": message,
        }
        if folder_uid:
            payload["folderUid"] = folder_uid
        else:
            payload["folderId"] = 0  # General
        return self._request("POST", "/api/dashboards/db", json=payload)

    def delete_dashboard(self, uid: str) -> None:
        self._request("DELETE", f"/api/dashboards/uid/{uid}")


def chunked(items: Iterable, size: int) -> Iterable[list]:
    buf: list = []
    for it in items:
        buf.append(it)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf
