import logging
import ssl
from typing import Any
from urllib.parse import urlparse

import httpx
from app.core.config import settings
from app.git_providers.utils.errors import GitProviderAccessTokenError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# Retry transient network errors with exponential backoff
_retry_transient = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
    reraise=True,
)


class BitbucketDCAPIResources:
    """API resources for Bitbucket Data Center/Server using HTTP Access Tokens.

    Key differences from Bitbucket Cloud:
    - API base: {instance}/rest/api/1.0 (not api.bitbucket.org/2.0)
    - Pagination: offset-based (start, limit) not cursor-based
    - Clone URL: https://:{token}@{host}/scm/{project}/{slug}.git (empty username)
    - Push event: repo:refs_changed (not repo:push)
    - PR merged event: pr:merged (not pullrequest:fulfilled)
    """

    def __init__(
        self,
        base_url: str,
        ca_bundle_path: str | None = None,
        disable_ssl_verify: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_base = f"{self.base_url}/rest/api/1.0"

        # Configure SSL context for self-signed certificates
        if disable_ssl_verify:
            if not settings.ALLOW_INSECURE_SSL_BITBUCKET_DC:
                raise ValueError(
                    "SSL verification cannot be disabled. Set ALLOW_INSECURE_SSL_BITBUCKET_DC=true "
                    "environment variable to allow insecure SSL for on-premise deployments."
                )
            logger.warning(
                "SSL verification disabled for Bitbucket DC API. "
                "This should only be used for development/testing."
            )
            self.verify: bool | ssl.SSLContext = False
        elif ca_bundle_path:
            self.verify = ssl.create_default_context()
            self.verify.load_verify_locations(ca_bundle_path)
        else:
            self.verify = True

    def get_current_user(self, access_token: str) -> dict[str, Any]:
        """Uses /plugins/servlet/applinks/whoami endpoint.
        Returns dict with at least 'name' or 'slug' field.
        """
        headers = {"Authorization": f"Bearer {access_token}"}

        # Try the application links whoami endpoint first (simpler, returns just username)
        whoami_url = f"{self.base_url}/plugins/servlet/applinks/whoami"

        try:
            with httpx.Client(verify=self.verify, timeout=30.0) as client:
                response = client.get(whoami_url, headers=headers)

                if response.status_code == 200:
                    username = response.text.strip()
                    if username:
                        return {"name": username, "slug": username}

                # Fallback to users endpoint with self-reference
                # Some versions of Bitbucket DC support /rest/api/1.0/users with no path
                users_url = f"{self.api_base}/users"
                response = client.get(
                    users_url, headers=headers, params={"filter": "", "limit": 1}
                )

                if response.status_code == 200:
                    data = response.json()
                    # The first user in the list when authenticated should be the current user
                    # But this isn't reliable, so we'll just use the first value if available
                    values = data.get("values", [])
                    if values:
                        return values[0]

                raise ValueError("Could not determine current user from token")

        except httpx.ConnectError as e:
            raise ValueError(
                f"Connection error: Unable to reach {self.base_url}"
            ) from e
        except httpx.TimeoutException as e:
            raise ValueError(f"Timeout reaching {self.base_url}") from e
        except httpx.HTTPStatusError as e:
            raise ValueError("HTTP error getting current user") from e

    def validate_token(self, access_token: str) -> tuple[bool, str]:
        """Validates by attempting to list repositories with Bearer auth."""
        try:
            headers = {"Authorization": f"Bearer {access_token}"}
            url = f"{self.api_base}/repos"

            with httpx.Client(verify=self.verify, timeout=30.0) as client:
                response = client.get(url, headers=headers, params={"limit": 1})

                if response.status_code == 200:
                    return True, "Valid token"
                elif response.status_code == 401:
                    return False, "Invalid or expired token"
                elif response.status_code == 403:
                    return False, "Token does not have sufficient permissions"
                else:
                    return False, f"Unexpected error: {response.status_code}"

        except httpx.HTTPError as e:
            return False, f"Connection error: {e}"

    @_retry_transient
    def list_repositories(
        self,
        access_token: str,
        project_key: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {access_token}"}
        repos = []

        try:
            if project_key:
                url = f"{self.api_base}/projects/{project_key}/repos"
            else:
                url = f"{self.api_base}/repos"

            start = 0

            with httpx.Client(verify=self.verify, timeout=60.0) as client:
                while True:
                    params = {"start": start, "limit": limit}
                    response = client.get(url, headers=headers, params=params)
                    response.raise_for_status()

                    data = response.json()
                    repos.extend(data.get("values", []))

                    # Check if this is the last page
                    if data.get("isLastPage", True):
                        break

                    # Move to next page
                    start = data.get("nextPageStart", start + limit)

            logger.info(f"Found {len(repos)} repositories")
            return repos

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise GitProviderAccessTokenError("Invalid HTTP access token") from e
            raise

    @_retry_transient
    def get_repository(
        self,
        project_key: str,
        repo_slug: str,
        access_token: str,
    ) -> dict[str, Any] | None:
        """Returns None if repository not found."""
        headers = {"Authorization": f"Bearer {access_token}"}
        url = f"{self.api_base}/projects/{project_key}/repos/{repo_slug}"

        try:
            with httpx.Client(verify=self.verify, timeout=30.0) as client:
                response = client.get(url, headers=headers)
                response.raise_for_status()
                return response.json()

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            if e.response.status_code == 401:
                raise GitProviderAccessTokenError("Invalid access token") from e
            raise

    @_retry_transient
    def get_default_branch(
        self,
        project_key: str,
        repo_slug: str,
        access_token: str,
    ) -> str | None:
        """Get default branch. Returns None if no default branch or no commits.

        We intentionally do NOT fall back to arbitrary branches if the default branch
        is missing or empty. If a repo has no configured default branch, that's a
        configuration issue that should be fixed in Bitbucket, not worked around here.
        """
        headers = {"Authorization": f"Bearer {access_token}"}
        url = f"{self.api_base}/projects/{project_key}/repos/{repo_slug}/default-branch"

        try:
            with httpx.Client(verify=self.verify, timeout=30.0) as client:
                response = client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()
                default_branch = data.get("displayId")

                if not default_branch:
                    logger.info(
                        f"No default branch configured for {project_key}/{repo_slug}"
                    )
                    return None

                # Verify the default branch has commits
                branch_url = (
                    f"{self.api_base}/projects/{project_key}/repos/{repo_slug}/branches"
                )
                branch_response = client.get(
                    branch_url,
                    headers=headers,
                    params={"filterText": default_branch, "limit": 1},
                )
                branch_response.raise_for_status()
                branches = branch_response.json().get("values", [])

                for b in branches:
                    if b.get("displayId") == default_branch:
                        if b.get("latestCommit"):
                            return default_branch
                        logger.info(
                            f"Default branch '{default_branch}' has no commits for {project_key}/{repo_slug}"
                        )
                        return None

                logger.info(
                    f"Default branch '{default_branch}' not found in branches list for {project_key}/{repo_slug}"
                )
                return None

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.info(
                    f"No default branch endpoint for {project_key}/{repo_slug} (empty repo)"
                )
                return None
            raise

    @_retry_transient
    def get_commit(
        self,
        project_key: str,
        repo_slug: str,
        commit_id: str,
        access_token: str,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {access_token}"}
        url = f"{self.api_base}/projects/{project_key}/repos/{repo_slug}/commits/{commit_id}"

        try:
            with httpx.Client(verify=self.verify, timeout=30.0) as client:
                response = client.get(url, headers=headers)
                response.raise_for_status()
                return response.json()

        except httpx.HTTPStatusError as e:
            e.add_note(f"Failed to get commit {commit_id}")
            raise

    def list_branches(
        self,
        project_key: str,
        repo_slug: str,
        access_token: str,
        filter_text: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {access_token}"}
        url = f"{self.api_base}/projects/{project_key}/repos/{repo_slug}/branches"
        branches = []

        try:
            start = 0

            with httpx.Client(verify=self.verify, timeout=30.0) as client:
                while True:
                    params: dict[str, Any] = {"start": start, "limit": limit}
                    if filter_text:
                        params["filterText"] = filter_text

                    response = client.get(url, headers=headers, params=params)
                    response.raise_for_status()

                    data = response.json()
                    branches.extend(data.get("values", []))

                    if data.get("isLastPage", True):
                        break

                    start = data.get("nextPageStart", start + limit)

            return branches

        except httpx.HTTPStatusError as e:
            e.add_note("Failed to list branches")
            raise

    def _build_webhook_payload(self, config: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": config["description"],
            "url": config["url"],
            "active": True,
            "events": config["events"],
            "configuration": {},
        }
        if "secret" in config:
            payload["configuration"]["secret"] = config["secret"]
        return payload

    @_retry_transient
    def create_repository_webhook(
        self,
        project_key: str,
        repo_slug: str,
        config: dict[str, Any],  # TODO: Use WebhookConfig. This requires more updates.
        access_token: str,
    ) -> dict[str, Any]:
        url = f"{self.api_base}/projects/{project_key}/repos/{repo_slug}/webhooks"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        payload = self._build_webhook_payload(config)

        try:
            with httpx.Client(verify=self.verify, timeout=30.0) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            e.add_note("Failed to create repository webhook")
            raise

    @_retry_transient
    def create_project_webhook(
        self,
        project_key: str,
        config: dict[str, Any],
        access_token: str,
    ) -> dict[str, Any]:
        """Fires for all repositories in the project. Requires Project Admin permissions."""
        url = f"{self.api_base}/projects/{project_key}/webhooks"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        payload = self._build_webhook_payload(config)

        try:
            with httpx.Client(verify=self.verify, timeout=30.0) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            e.add_note("Failed to create project webhook")
            raise

    def delete_repository_webhook(
        self,
        project_key: str,
        repo_slug: str,
        webhook_id: int,
        access_token: str,
    ) -> None:
        url = f"{self.api_base}/projects/{project_key}/repos/{repo_slug}/webhooks/{webhook_id}"
        headers = {"Authorization": f"Bearer {access_token}"}

        try:
            with httpx.Client(verify=self.verify, timeout=30.0) as client:
                response = client.delete(url, headers=headers)
                response.raise_for_status()
        except httpx.HTTPStatusError as e:
            e.add_note("Failed to delete repository webhook")
            raise

    def delete_project_webhook(
        self,
        project_key: str,
        webhook_id: int,
        access_token: str,
    ) -> None:
        url = f"{self.api_base}/projects/{project_key}/webhooks/{webhook_id}"
        headers = {"Authorization": f"Bearer {access_token}"}

        try:
            with httpx.Client(verify=self.verify, timeout=30.0) as client:
                response = client.delete(url, headers=headers)
                response.raise_for_status()
        except httpx.HTTPStatusError as e:
            e.add_note("Failed to delete project webhook")
            raise

    def build_clone_url(
        self,
        project_key: str,
        repo_slug: str,
    ) -> str:
        """Token must be passed via git header: git clone -c http.extraHeader='Authorization: Bearer TOKEN'"""
        parsed = urlparse(self.base_url)
        host = parsed.netloc
        scheme = parsed.scheme

        return f"{scheme}://{host}/scm/{project_key}/{repo_slug}.git"
