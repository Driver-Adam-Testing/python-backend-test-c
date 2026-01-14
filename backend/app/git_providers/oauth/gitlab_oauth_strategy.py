import logging
from urllib.parse import urlencode

import httpx
from app.git_providers.core.config import GitProviderConfig
from app.git_providers.utils.errors import GitProviderAppRevokeError

logger = logging.getLogger(__name__)


class GitLabOAuthStrategy:
    def __init__(self, config: GitProviderConfig) -> None:
        self.config = config

    def _make_post_request(self, url: str, payload: dict) -> dict:
        with httpx.Client() as client:
            response = client.post(url, data=payload)
            response.raise_for_status()
            return response.json()

    def _token_info(self, token: str) -> dict:
        url = f"{self.config.base_url}/{self.config.token_info_endpoint}"
        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client() as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()  # Raises an exception if the HTTP response status is not successful.
            return response.json()

    def token_user(self, token: str) -> dict:
        url = f"{self.config.base_url}/{self.config.user_endpoint}"
        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client() as client:
            response = client.get(url, headers=headers, follow_redirects=True)
            response.raise_for_status()  # Raises an exception if the HTTP response status is not successful.
            return response.json()

    def generate_authorization_url(self, state: str) -> str:
        query_params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            "scope": self.config.scope,
            "state": state,
        }
        return f"{self.config.authorize_url}?{urlencode(query_params)}"

    def exchange_code_for_token(self, code: str) -> dict:
        payload = {
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.config.redirect_uri,
        }
        return self._make_post_request(self.config.access_token_url, payload)

    def refresh_access_token(self, refresh_token: str) -> dict:
        try:
            payload = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
            }
            return self._make_post_request(self.config.access_token_url, payload)
        except httpx.HTTPStatusError as e:
            if (
                e.response.status_code == 400
                and e.response.json()["error"] == "invalid_grant"
            ):
                logger.error("Access token revoked or expired")
                # this token was either revoked or expired and thus we need to force the user to re-authenticate/reinstall the app
                raise GitProviderAppRevokeError("Access token revoked or expired", e)
            else:
                raise

    def is_token_valid(self, token: str) -> bool:
        try:
            token_info = self._token_info(token)
            expires_in = token_info["expires_in"]
            return expires_in > 0
        except httpx.HTTPStatusError as e:
            if (
                e.response.status_code == 401
                and e.response.json()["error"] == "invalid_token"
            ):
                # Token is invalid we can still potentially refresh it
                return False
            else:
                raise
