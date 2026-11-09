"""HTTP client for Kalku's connector API (``/api/connector``).

A plain Python class, independent of the ORM, so it can be tested with a mocked
``requests.Session``.
"""

import time

import requests

TIMEOUT = 60
RETRY_AFTER_DEFAULT = 30
RETRY_AFTER_MAX = 120


class KalkuError(Exception):
    """Kalku refused a request or could not be reached (``status`` is None then)."""

    def __init__(self, status, message, errors=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.errors = errors


class KalkuAuthError(KalkuError):
    """401 or 403: the token was revoked, lacks its scope, or its owner is inactive."""


class KalkuClient:
    def __init__(self, url, token, session=None):
        self.base_url = (url or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self.session = session or requests.Session()

    def ping(self):
        return self._request("GET", "/api/connector/ping")

    def push_sales(self, payload):
        return self._request("POST", "/api/connector/sales", payload)

    def push_inventory(self, rows):
        return self._request("POST", "/api/connector/inventory", {"rows": rows})

    def push_supplier_terms(self, rows):
        return self._request("POST", "/api/connector/supplier-terms", {"rows": rows})

    def complete(self, payload):
        return self._request("POST", "/api/connector/sync/complete", payload)

    def _request(self, method, path, payload=None):
        response = self._send(method, path, payload)
        if response.status_code == 429:
            # throttle:60,1 — wait what Kalku asks (bounded) and try once more.
            time.sleep(self._retry_after(response))
            response = self._send(method, path, payload)
        if 200 <= response.status_code < 300:
            body = self._json(response)
            return body if isinstance(body, dict) else {}
        body = self._json(response)
        body = body if isinstance(body, dict) else {}
        message = body.get("message") or "Kalku answered HTTP %s." % response.status_code
        error_class = KalkuAuthError if response.status_code in (401, 403) else KalkuError
        raise error_class(response.status_code, message, body.get("errors"))

    def _send(self, method, path, payload):
        try:
            return self.session.request(
                method,
                self.base_url + path,
                json=payload,
                headers={
                    "Authorization": "Bearer %s" % self.token,
                    "Accept": "application/json",
                },
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise KalkuError(None, "Kalku could not be reached: %s" % exc) from exc

    @staticmethod
    def _retry_after(response):
        try:
            seconds = int(response.headers.get("Retry-After", RETRY_AFTER_DEFAULT))
        except (TypeError, ValueError):
            seconds = RETRY_AFTER_DEFAULT
        return max(0, min(seconds, RETRY_AFTER_MAX))

    @staticmethod
    def _json(response):
        try:
            return response.json()
        except ValueError:
            return None
