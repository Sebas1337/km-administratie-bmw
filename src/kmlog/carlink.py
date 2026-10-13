"""Linking the car from the web UI: BMW CarData device-code login in steps, no local scripts.

1. start: the user enters the CarData client id (from the My BMW portal); we request a device
   code and keep it (with the PKCE verifier) as `pending_login` in the token state.
2. The user opens BMW's verification link, logs in and approves.
3. confirm: one token request. On success the tokens are stored and the vehicle mappings are
   fetched; with exactly one PRIMARY vehicle its VIN is chosen, otherwise the user picks one.
4. unlink: delete our telematic container (best effort) and forget tokens, VIN and container;
   the client id is kept so linking again only needs the BMW approval.

Every CarData API call counts against the daily quota (the caller passes before_api_call).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .cardata import AuthError, CarDataClient, CarDataError, DeviceLogin


class LinkError(RuntimeError):
    """Shown to the user as is (Dutch)."""


@dataclass
class LinkStatus:
    client_id: str | None
    linked: bool
    vin: str | None
    pending: DeviceLogin | None
    vehicles: list[dict]  # [{"vin", "mappingType"}] while a choice is needed
    container_id: str | None


def status(client: CarDataClient, clock: Callable[[], float] = time.time) -> LinkStatus:
    s = client.state
    pending = DeviceLogin.from_dict(s["pending_login"]) if s.get("pending_login") else None
    if pending and pending.expires_at <= clock():
        pending = None
    return LinkStatus(
        client_id=s.get("client_id"),
        linked=client.linked,
        vin=s.get("vin"),
        pending=pending,
        vehicles=s.get("vehicles") or [],
        container_id=s.get("container_id"),
    )


def start(client: CarDataClient, client_id: str, clock: Callable[[], float] = time.time) -> None:
    client_id = client_id.strip()
    if not client_id or len(client_id) > 200:
        raise LinkError("Vul de CarData Client ID in (uit het My BMW-portaal).")
    if client.linked:
        raise LinkError("Er is al een auto gekoppeld; ontkoppel die eerst.")
    client.state = {"client_id": client_id}
    try:
        login = client.start_device_login()
    except AuthError as e:
        raise LinkError(
            "BMW weigert de aanvraag. Controleer de Client ID en of de client in het portaal "
            "geabonneerd is op 'CarData API'."
        ) from e
    login.expires_at = clock() + login.expires_in
    client.state["pending_login"] = login.to_dict()
    client._save()


def confirm(client: CarDataClient, clock: Callable[[], float] = time.time) -> str:
    """'linked', 'choose' (several vehicles) or 'pending' (not approved at BMW yet)."""
    st = status(client, clock)
    if st.pending is None:
        raise LinkError("Geen lopende koppeling (of de code is verlopen); begin opnieuw.")
    try:
        result = client.poll_device_login(st.pending)
    except AuthError as e:
        raise LinkError("BMW weigert de koppeling; begin opnieuw.") from e
    if result != "ok":
        return "pending"
    client.state.pop("pending_login", None)
    client._save()
    try:
        mappings = client.mappings()
    except CarDataError as e:
        raise LinkError(
            "Gekoppeld, maar de voertuigen konden niet worden opgehaald; probeer het later."
        ) from e
    primary = [m["vin"] for m in mappings if m.get("mappingType") == "PRIMARY"]
    if not primary:
        raise LinkError(
            "Gekoppeld, maar dit BMW-account is van geen enkele auto de hoofdgebruiker "
            "(PRIMARY); CarData levert alleen gegevens aan de hoofdgebruiker."
        )
    if len(primary) == 1:
        client.state["vin"] = primary[0]
        client._save()
        return "linked"
    client.state["vehicles"] = [{"vin": v, "mappingType": "PRIMARY"} for v in primary]
    client._save()
    return "choose"


def choose_vehicle(client: CarDataClient, vin: str) -> None:
    if vin not in {v["vin"] for v in client.state.get("vehicles") or []}:
        raise LinkError("Onbekend voertuig.")
    client.state["vin"] = vin
    client.state.pop("vehicles", None)
    client._save()


def cancel(client: CarDataClient) -> None:
    client.state.pop("pending_login", None)
    client._save()


def unlink(client: CarDataClient) -> None:
    container = client.state.get("container_id")
    if container and client.linked:
        try:  # max 10 containers per account, so clean up; failing here is harmless
            client.request("DELETE", f"/customers/containers/{container}")
        except CarDataError:
            pass
    client.state = {"client_id": client.state.get("client_id")}
    client._save()
