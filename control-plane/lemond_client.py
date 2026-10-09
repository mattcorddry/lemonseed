"""Thin client for the Lemonade (lemond) HTTP API.

The control plane talks to lemond over its HTTP API rather than the
`lemond`/`lemonade` CLI wherever possible (see DESIGN.md "lemond API over
CLI"). We build on the unofficial `lemonade-python-sdk` (pure-Python, depends
only on `requests`) and subclass `LemonadeClient` to add the few endpoints it
lacks:

  - delete_model(name)      POST /v1/delete          (unloads first if loaded)
  - system_info()           GET  /v1/system-info     (model_storage free/total)
  - model_files(name)       GET  /v1/models/{id}/files (per-model size in bytes)
  - loaded_model_names()    GET  /v1/health          (models resident in memory)

The base URL and API key come from the same sources the rest of the control
plane uses (lemond on 127.0.0.1:13305; LEMONADE_API_KEY from lemond.env). When
a key is present it is attached to the shared `requests.Session` as a Bearer
header so every request — including the SDK's own — is authenticated.
"""

from __future__ import annotations

from typing import Any, Optional

try:  # The SDK ships in the appliance image; unit tests run without it.
    from lemonade_sdk import LemonadeClient
except Exception:  # noqa: BLE001
    LemonadeClient = None  # type: ignore[assignment, misc]

from hardware import lemonade_api_key

# lemond is bound to localhost on the appliance (never a LAN interface).
LEMOND_BASE_URL = "http://127.0.0.1:13305"

if LemonadeClient is not None:

    class LemondClient(LemonadeClient):
        """LemonadeClient extended with the endpoints the SDK does not provide."""

        def __init__(self, base_url: str = LEMOND_BASE_URL, api_key: str = "") -> None:
            super().__init__(base_url=base_url)
            if api_key:
                self.session.headers["Authorization"] = f"Bearer {api_key}"

        def delete_model(self, model_name: str) -> bool:
            """Delete a model via POST /v1/delete.

            lemond unloads the model first if it is loaded, so this is the only
            safe way to remove a model from underneath a running lemond (a raw
            directory delete would race the server). Returns True on success.
            """
            url = f"{self.base_url}/v1/delete"
            try:
                response = self.session.post(
                    url, json={"model_name": model_name}, timeout=30
                )
                response.raise_for_status()
                return True
            except Exception:  # noqa: BLE001
                return False

        def system_info(self) -> Optional[dict[str, Any]]:
            """GET /v1/system-info, or None when lemond is unreachable.

            The payload includes `model_storage: {path, used_bytes, total_bytes,
            free_bytes}` describing the drive that holds the model cache.
            """
            url = f"{self.base_url}/v1/system-info"
            try:
                response = self.session.get(url, timeout=5)
                response.raise_for_status()
                data = response.json()
                return data if isinstance(data, dict) else None
            except Exception:  # noqa: BLE001
                return None

        def model_files(self, model_name: str) -> Optional[list[dict[str, Any]]]:
            """GET /v1/models/{id}/files → the model's file list, or None.

            Each entry carries `size_bytes`; summing them (directories are
            reported recursively by lemond) yields the model's on-disk size.
            """
            url = f"{self.base_url}/v1/models/{model_name}/files"
            try:
                response = self.session.get(url, timeout=10)
                response.raise_for_status()
                data = response.json()
                files = data.get("files") if isinstance(data, dict) else None
                return files if isinstance(files, list) else None
            except Exception:  # noqa: BLE001
                return None

        def loaded_model_names(self) -> set[str]:
            """Names currently resident in lemond (not merely downloaded).

            Reads /v1/health and extracts `model_loaded` plus
            `all_models_loaded[].model_name`. Returns an empty set when lemond
            is unreachable (caller then cannot distinguish loaded from
            unloaded, so it should be conservative).
            """
            url = f"{self.base_url}/v1/health"
            try:
                response = self.session.get(url, timeout=5)
                response.raise_for_status()
                data = response.json()
            except Exception:  # noqa: BLE001
                return set()
            if not isinstance(data, dict):
                return set()
            names: set[str] = set()
            single = data.get("model_loaded")
            if isinstance(single, str) and single:
                names.add(single)
            multi = data.get("all_models_loaded")
            if isinstance(multi, list):
                for entry in multi:
                    if isinstance(entry, str) and entry:
                        names.add(entry)
                    elif isinstance(entry, dict):
                        for key in ("model_name", "model", "id", "name"):
                            value = entry.get(key)
                            if isinstance(value, str) and value:
                                names.add(value)
                                break
            return names


def get_client() -> Optional["LemonadeClient"]:
    """Construct a LemondClient wired to the appliance's lemond instance.

    Returns None when the SDK is unavailable (e.g. in the unit-test
    environment); callers then use the pure-filesystem path.
    """
    if LemonadeClient is None:
        return None
    return LemondClient(base_url=LEMOND_BASE_URL, api_key=lemonade_api_key())
