from __future__ import annotations

import base64
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import httpx

BASE_URL = "https://api.kroger.com/v1"
AUTHORIZE_PATH = "/connect/oauth2/authorize"
TOKEN_PATH = "/connect/oauth2/token"

CART_SCOPE = "cart.basic:write"


class KrogerError(Exception):
    """Any non-auth API failure."""


class KrogerAuthError(KrogerError):
    """Auth-specific failure (token exchange, refresh, missing scope)."""


@dataclass(frozen=True)
class ClientCreds:
    client_id: str
    client_secret: str

    def basic_auth(self) -> str:
        raw = f"{self.client_id}:{self.client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()


class KrogerClient:
    """
    Kroger API client.

    Phase 1 covers the client-credentials flow (product.compact scope) for
    /locations and /products. Cart writes and the authorization-code flow
    land in phase 2.
    """

    def __init__(self, creds: ClientCreds, *, timeout: float = 20.0):
        self.creds = creds
        self._client = httpx.Client(base_url=BASE_URL, timeout=timeout)
        self._app_token: Optional[str] = None
        self._app_token_exp: float = 0.0

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _get_app_token(self) -> str:
        if self._app_token and time.time() < self._app_token_exp - 30:
            return self._app_token
        resp = self._client.post(
            TOKEN_PATH,
            headers={
                "Authorization": self.creds.basic_auth(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": "product.compact"},
        )
        if resp.status_code == 401:
            raise KrogerAuthError(
                "Token request rejected (401). Check KROGER_CLIENT_ID / "
                "KROGER_CLIENT_SECRET and that the app is in Production."
            )
        if resp.status_code != 200:
            raise KrogerAuthError(
                f"Token request failed: HTTP {resp.status_code} {resp.text[:400]}"
            )
        body = resp.json()
        self._app_token = body["access_token"]
        self._app_token_exp = time.time() + int(body.get("expires_in", 1800))
        return self._app_token

    def _get(self, path: str, params: dict) -> dict:
        token = self._get_app_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        resp = self._client.get(path, headers=headers, params=params)
        if resp.status_code == 401:
            self._app_token = None
            token = self._get_app_token()
            headers["Authorization"] = f"Bearer {token}"
            resp = self._client.get(path, headers=headers, params=params)
        if resp.status_code == 403 and "scope" in resp.text.lower():
            raise KrogerAuthError(
                f"403 on {path}: missing required scope. "
                "Enable the relevant API product at developer.kroger.com."
            )
        if resp.status_code >= 400:
            raise KrogerError(
                f"GET {path} failed: HTTP {resp.status_code} {resp.text[:400]}"
            )
        return resp.json()

    def find_locations(
        self,
        *,
        zip_code: str,
        radius: int = 10,
        limit: int = 25,
        chain: Optional[str] = None,
    ) -> list[dict]:
        params: dict = {
            "filter.zipCode.near": zip_code,
            "filter.radiusInMiles": radius,
            "filter.limit": limit,
        }
        if chain:
            params["filter.chain"] = chain
        data = self._get("/locations", params)
        return data.get("data") or []

    def get_location(self, location_id: str) -> dict:
        data = self._get(f"/locations/{location_id}", {})
        return data.get("data") or {}

    def search_products(
        self,
        *,
        location_id: str,
        term: Optional[str] = None,
        brand: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        params: dict = {
            "filter.locationId": location_id,
            "filter.limit": limit,
        }
        if term:
            params["filter.term"] = term
        if brand:
            params["filter.brand"] = brand
        data = self._get("/products", params)
        return data.get("data") or []

    # ---- user-authorization (cart write) ----

    def build_authorize_url(self, *, redirect_uri: str, state: str, scope: str = CART_SCOPE) -> str:
        qs = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": self.creds.client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
        })
        return f"{BASE_URL}{AUTHORIZE_PATH}?{qs}"

    def exchange_auth_code(self, *, code: str, redirect_uri: str) -> dict:
        resp = self._client.post(
            TOKEN_PATH,
            headers={
                "Authorization": self.creds.basic_auth(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        if resp.status_code != 200:
            raise KrogerAuthError(
                f"Code exchange failed: HTTP {resp.status_code} {resp.text[:400]}"
            )
        body = resp.json()
        if "access_token" not in body or "refresh_token" not in body:
            raise KrogerAuthError(f"Code exchange missing tokens: {body}")
        return body

    def refresh_user_token(self, refresh_token: str) -> dict:
        resp = self._client.post(
            TOKEN_PATH,
            headers={
                "Authorization": self.creds.basic_auth(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
        if resp.status_code != 200:
            raise KrogerAuthError(
                f"Refresh failed: HTTP {resp.status_code} {resp.text[:400]}"
            )
        body = resp.json()
        if "access_token" not in body:
            raise KrogerAuthError(f"Refresh missing access_token: {body}")
        return body

    def add_to_cart(self, *, user_access_token: str, upc: str, quantity: int, modality: str) -> None:
        resp = self._client.put(
            "/cart/add",
            headers={
                "Authorization": f"Bearer {user_access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={"items": [{"upc": upc, "quantity": quantity, "modality": modality}]},
        )
        if resp.status_code == 204:
            return
        if resp.status_code == 401:
            raise KrogerAuthError("Cart add rejected (401). Token may be expired or revoked.")
        if resp.status_code == 403 and "scope" in resp.text.lower():
            raise KrogerAuthError(
                "Cart add rejected (403 missing scope). Re-run `buzzcheck auth --force`."
            )
        raise KrogerError(
            f"Cart add failed: HTTP {resp.status_code} {resp.text[:400]}"
        )
