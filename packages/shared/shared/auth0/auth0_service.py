import logging
import time
from typing import Any

import httpx
from auth0.authentication import Database, GetToken, Users
from auth0.management import Auth0
from auth0.rest import RestClientOptions

from shared.auth0.models import User as UserToken
from shared.auth0.schemas import (
    CreateInvitationInput,
    ModifyUserRolesResponse,
)

logger = logging.getLogger(__name__)


class Auth0Service:
    _mgmt_token: str | None = None
    _mgmt_token_exp: float = 0.0  # epoch seconds

    # Cached values (class-level, shared across instances)
    _cached_username_password_connection_id: str | None = None
    _cached_admin_role_id: str | None = None

    def __init__(
        self,
        auth0_mgmt_domain: str,
        auth0_mgmt_client_id: str,
        auth0_mgmt_client_secret: str,
        auth0_domain: str,
        auth0_client_id: str,
        timeout: float = 30.0,
    ) -> None:
        self.auth0_mgmt_domain = auth0_mgmt_domain
        self.auth0_mgmt_client_id = auth0_mgmt_client_id
        self.auth0_mgmt_client_secret = auth0_mgmt_client_secret
        self.auth0_domain = auth0_domain
        self.auth0_client_id = auth0_client_id
        self.timeout = timeout

    def _refresh_management_token(self) -> None:
        get_token = GetToken(
            self.auth0_mgmt_domain,
            client_id=self.auth0_mgmt_client_id,
            client_secret=self.auth0_mgmt_client_secret,
        )
        token = get_token.client_credentials(
            f"https://{self.auth0_mgmt_domain}/api/v2/"
        )
        self._mgmt_token = token["access_token"]
        self._mgmt_token_exp = time.time() + token.get("expires_in", 86_400)

    def _management_client(self) -> Auth0:
        if self._mgmt_token is None or self._mgmt_token_exp - time.time() < 60:
            self._refresh_management_token()

        return Auth0(
            self.auth0_mgmt_domain,
            self._mgmt_token,
            rest_options=RestClientOptions(
                telemetry=False,
                timeout=self.timeout,
            ),
        )

    def get_mgmt_api_token(self: "Auth0Service") -> str:
        get_token = GetToken(
            self.auth0_mgmt_domain,
            client_id=self.auth0_mgmt_client_id,
            client_secret=self.auth0_mgmt_client_secret,
        )
        token = get_token.client_credentials(
            f"https://{self.auth0_mgmt_domain}/api/v2/"
        )
        return token["access_token"]

    def change_self_password(
        self: "Auth0Service", user: UserToken, access_token: str
    ) -> str:
        users = Users(domain=self.auth0_domain)
        try:
            user_profile = users.userinfo(access_token)
            management_domain = self.auth0_mgmt_domain
            db = Database(management_domain, self.auth0_client_id)
            response = db.change_password(
                email=user_profile.get("email"),
                connection="Username-Password-Authentication",
                organization=user.organization_id,
            )
            logger.info(
                f"User requested password reset for {user.subject} sent to {user_profile.get('email')}"
            )
            return response
        except Exception as e:
            logger.error(f"An error occurred getting user's information: {e}")
            raise e

    def list_user_organizations(self: "Auth0Service", user: UserToken) -> any:
        try:
            mgmt_api_token = self.get_mgmt_api_token()
            management_api = Auth0(self.auth0_mgmt_domain, mgmt_api_token)

            return management_api.users.list_organizations(user.user_id, per_page=100)
        except Exception as e:
            logger.error(
                f"Something went wrong listing organizations for {user.user_id}"
            )
            raise e

    def modify_user_roles(
        self: "Auth0Service",
        user: UserToken,
        roles: list[str],
        modified_user_id: str,
    ) -> any:
        try:
            mgmt_api_token = self.get_mgmt_api_token()
            management_api = Auth0(self.auth0_mgmt_domain, mgmt_api_token)
            existing_roles = management_api.organizations.all_organization_member_roles(
                id=user.organization_id, user_id=modified_user_id
            )
            existing_role_ids = [r["id"] for r in existing_roles]
            new_role_ids = []
            for added_role in roles:
                if added_role not in existing_role_ids:
                    new_role_ids.append(added_role)
            if len(new_role_ids) > 0:
                management_api.organizations.create_organization_member_roles(
                    id=user.organization_id,
                    user_id=modified_user_id,
                    body={"roles": new_role_ids},
                )

            removed_role_ids = []
            for existing_role_id in existing_role_ids:
                if existing_role_id not in roles:
                    removed_role_ids.append(existing_role_id)
            if len(removed_role_ids) > 0:
                management_api.organizations.delete_organization_member_roles(
                    id=user.organization_id,
                    user_id=modified_user_id,
                    body={"roles": removed_role_ids},
                )

            return ModifyUserRolesResponse(
                user_id=modified_user_id,
                added_roles=new_role_ids,
                removed_roles=removed_role_ids,
            )
        except Exception as e:
            logger.error(f"Something went wrong modifying user roles {user.user_id}")
            raise e

    def list_members(
        self: "Auth0Service", user: UserToken, page: int = 0, per_page: int = 100
    ) -> any:
        try:
            mgmt_api_token = self.get_mgmt_api_token()
            management_api = Auth0(self.auth0_mgmt_domain, mgmt_api_token)
            return management_api.organizations.all_organization_members(
                id=user.organization_id,
                page=page,
                per_page=per_page,
                # Roles are not returned by default, so we have to do this explicitly
                fields=["user_id", "email", "picture", "name", "roles"],
            )
        except Exception as e:
            logger.error(
                f"Something went wrong listing users for organization {user.organization_display_name} requested by {user.user_id}"
            )
            raise e

    def list_invitations(
        self: "Auth0Service", user: UserToken, page: int = 0, per_page: int = 100
    ) -> any:
        try:
            mgmt_api_token = self.get_mgmt_api_token()
            management_api = Auth0(self.auth0_mgmt_domain, mgmt_api_token)
            return management_api.organizations.all_organization_invitations(
                id=user.organization_id, page=page, per_page=per_page
            )
        except Exception as e:
            logger.error(
                f"Something went wrong listing invitations for organization {user.organization_display_name} requested by {user.user_id}"
            )
            raise e

    def list_roles(self: "Auth0Service", page: int = 0, per_page: int = 100) -> any:
        try:
            mgmt_api_token = self.get_mgmt_api_token()
            management_api = Auth0(self.auth0_mgmt_domain, mgmt_api_token)
            return management_api.roles.list(page=page, per_page=per_page)
        except Exception as e:
            logger.error("Something went wrong listing roles")
            raise e

    def get_role_id_by_name(self: "Auth0Service", role_name: str) -> str | None:
        """
        Lookup a role ID by its name (exact match). Returns None if not found.
        """
        try:
            mgmt_api_token = self.get_mgmt_api_token()
            management_api = Auth0(self.auth0_mgmt_domain, mgmt_api_token)
            page = 0
            per_page = 50
            while True:
                roles_page = management_api.roles.list(page=page, per_page=per_page)

                # Normalize SDK response shapes
                items: list = []
                if isinstance(roles_page, list):
                    items = roles_page
                elif isinstance(roles_page, dict):
                    for key in ("roles", "items", "results", "data", "list"):
                        if isinstance(roles_page.get(key), list):
                            items = roles_page.get(key)  # type: ignore[assignment]
                            break
                    if not items:
                        # Some SDKs return {'length': n, 'start': 0, 'limit': 50, 'roles': [...]}
                        # Already handled above; if still empty, nothing to iterate.
                        items = []

                if not items:
                    return None

                for item in items:
                    if isinstance(item, dict):
                        if item.get("name") == role_name:
                            return item.get("id")
                    elif isinstance(item, str):
                        # If the SDK returns IDs as strings, fetch details
                        try:
                            role_obj = management_api.roles.get(item)
                            if role_obj and role_obj.get("name") == role_name:
                                return role_obj.get("id")
                        except Exception:
                            # Ignore and continue searching
                            pass

                if len(items) < per_page:
                    return None
                page += 1
        except Exception:
            logger.error(f"Error looking up role by name '{role_name}'", exc_info=True)
            return None

    def create_invitation(
        self: "Auth0Service",
        user: UserToken,
        access_token: str,
        invitations: CreateInvitationInput,
    ) -> any:
        users = Users(domain=self.auth0_domain)
        try:
            userinfo = users.userinfo(access_token)
            mgmt_api_token = self.get_mgmt_api_token()
            management_api = Auth0(self.auth0_mgmt_domain, mgmt_api_token)

            organization_info = management_api.organizations.get_organization(
                user.organization_id
            )

            invitation_results = []
            for invitation in invitations.invitations:
                payload = {
                    "inviter": {"name": userinfo.get("name")},
                    "invitee": invitation.invitee.model_dump(),
                    "client_id": self.auth0_client_id,
                    "app_metadata": {
                        user.organization_id: {
                            "initial_org_role": invitation.role.value
                        }
                    },
                }

                if (
                    "metadata" in organization_info
                    and "sso_connection_id" in organization_info["metadata"]
                ):
                    payload["connection_id"] = organization_info["metadata"][
                        "sso_connection_id"
                    ]
                invitation_results.append(
                    management_api.organizations.create_organization_invitation(
                        id=user.organization_id,
                        body=payload,
                    )
                )
            invitees_info = [
                {"email": inv.invitee.email, "role": inv.role.value}
                for inv in invitations.invitations
            ]
            logger.info(
                "RBAC mutation: action=%s, user_id=%s, org_id=%s, org_name=%s, invitees=%s",
                "invitation.create",
                user.user_id,
                user.organization_id,
                user.organization_display_name,
                invitees_info,
            )
            return invitation_results
        except Exception as e:
            logger.error(
                f"Something went wrong creating the invitation for organization {user.organization_display_name} requested by {user.user_id}"
            )
            raise e

    def create_admin_invite(
        self: "Auth0Service",
        org_id: str,
        email: str,
        role_name: str,
    ) -> any:
        """
        Creates an admin invitation for a newly created organization.
        Replicates logic from create_invitation but adapted for system/admin use.
        """
        try:
            mgmt_api_token = self.get_mgmt_api_token()
            management_api = Auth0(self.auth0_mgmt_domain, mgmt_api_token)

            organization_info = management_api.organizations.get_organization(org_id)

            payload = {
                "inviter": {"name": "Driver Admin"},
                "invitee": {"email": email},
                "client_id": self.auth0_client_id,
                "app_metadata": {org_id: {"initial_org_role": role_name}},
            }

            if (
                "metadata" in organization_info
                and "sso_connection_id" in organization_info["metadata"]
            ):
                payload["connection_id"] = organization_info["metadata"][
                    "sso_connection_id"
                ]

            return management_api.organizations.create_organization_invitation(
                id=org_id,
                body=payload,
            )
        except Exception as e:
            logger.error(
                f"Something went wrong creating the admin invitation for organization {org_id}"
            )
            raise e

    def delete_user_from_organization(
        self: "Auth0Service", user: UserToken, user_id_to_remove: str
    ) -> any:
        try:
            mgmt_api_token = self.get_mgmt_api_token()
            management_api = Auth0(self.auth0_mgmt_domain, mgmt_api_token)
            return management_api.organizations.delete_organization_members(
                id=user.organization_id,
                body={"members": [user_id_to_remove]},
            )
        except Exception as e:
            logger.error(
                f"Something went wrong removing the member {user.user_id} from the organization {user.organization_id}"
            )
            raise e

    def delete_invitation(
        self: "Auth0Service", user: UserToken, invitation_id: str
    ) -> any:
        try:
            mgmt_api_token = self.get_mgmt_api_token()
            management_api = Auth0(self.auth0_mgmt_domain, mgmt_api_token)
            result = management_api.organizations.delete_organization_invitation(
                id=user.organization_id, invitation_id=invitation_id
            )
            logger.info(
                "RBAC mutation: action=%s, user_id=%s, org_id=%s, org_name=%s, invitation_id=%s",
                "invitation.delete",
                user.user_id,
                user.organization_id,
                user.organization_display_name,
                invitation_id,
            )
            return result
        except Exception as e:
            logger.error(
                f"Something went wrong revoking invitation id = {invitation_id} from the organization {user.organization_id}"
            )
            raise e

    def get_user_profile(self, user_id: str) -> dict[str, any]:
        """
        Return a single user profile from Auth0 Management API.
        """
        client = self._management_client()
        return client.users.get(user_id)

    def get_organization(self, org_id: str) -> dict[str, any]:
        """
        Return the Auth0 Organization object for *org_id*.

        Requires the Management API scope:  read:organizations
        """
        client = self._management_client()
        return client.organizations.get_organization(org_id)

    def get_user_organizations(self, user_id: str) -> list[dict[str, Any]]:
        client = self._management_client()

        organizations = []
        page = 0
        per_page = 100

        while True:
            response = client.users.list_organizations(
                user_id, per_page=per_page, page=page
            )
            batch = response.get("organizations", [])

            if not batch:
                break

            organizations.extend(batch)
            page += 1

            # Safety break to avoid infinite loops
            if len(batch) < per_page:
                break

        return organizations

    # ------------------------------------------------------------------
    #  Public signup helpers (no existing user context)
    # ------------------------------------------------------------------

    def find_users_by_email(self, email: str) -> list[dict[str, any]]:
        """
        Find Auth0 users by email via Management API. Returns a list; empty if not found.
        Requires scope: read:users or read:users_app_metadata
        """
        try:
            mgmt_token = self.get_mgmt_api_token()
            url = f"https://{self.auth0_mgmt_domain}/api/v2/users-by-email"
            params = {"email": email}
            headers = {
                "Authorization": f"Bearer {mgmt_token}",
                "Content-Type": "application/json",
            }

            resp = httpx.get(url, params=params, headers=headers, timeout=15)

            if resp.status_code == 200:
                users = resp.json()
                return users if isinstance(users, list) else []
            else:
                resp.raise_for_status()

        except Exception as e:
            logger.error(f"Error finding users by email '{email}': {e!s}")
            raise

    def create_organization(
        self, name: str, display_name: str, metadata: dict[str, any] | None = None
    ) -> dict[str, any]:
        """
        Create an Auth0 Organization.

        Requires Management API scope: create:organizations
        """
        client = self._management_client()
        body_dict: dict[str, any] = {
            "name": name,
            "display_name": display_name,
        }
        if metadata:
            body_dict["metadata"] = metadata
        return client.organizations.create_organization(body_dict)

    def delete_organization(self, org_id: str) -> None:
        """Delete an Auth0 Organization."""
        client = self._management_client()
        client.organizations.delete_organization(org_id)

    def invite_email_to_organization(
        self,
        org_id: str,
        email: str,
        roles: list[str] | None = None,
        inviter_name: str | None = None,
        send_invitation_email: bool = True,
    ) -> dict[str, any]:
        """
        Create an organization invitation for an email address.

        Requires Management API scopes: create:organization_invitations
        """
        client = self._management_client()
        # Optionally include SSO connection if present in org metadata
        org = client.organizations.get_organization(org_id)
        payload: dict[str, any] = {
            "inviter": {"name": inviter_name or "System"},
            "invitee": {"email": email},
            "client_id": self.auth0_client_id,
        }
        # Suppress Auth0 emailing the invite if requested
        if send_invitation_email is False:
            payload["send_invitation_email"] = False
        if roles:
            payload["roles"] = roles
        if "metadata" in org and isinstance(org["metadata"], dict):
            connection_id = org["metadata"].get("sso_connection_id")
            if connection_id:
                payload["connection_id"] = connection_id

        return client.organizations.create_organization_invitation(
            id=org_id, body=payload
        )

    # ------------------------------------------------------------------
    #  Organization Connection helpers
    # ------------------------------------------------------------------

    def get_connection_id_by_name(self, name: str) -> str | None:
        """Return a connection ID for a given connection name, or None if not found."""
        client = self._management_client()
        page = 0
        per_page = 50
        while True:
            connections = client.connections.all(page=page, per_page=per_page)
            if not connections:
                return None
            for conn in connections:
                if conn.get("name") == name:
                    return conn.get("id")
            if len(connections) < per_page:
                return None
            page += 1

    def enable_connection_for_organization(
        self, org_id: str, connection_id: str
    ) -> None:
        """
        Enable a connection on an organization.

        Requires Management API scopes: update:organizations, read:connections
        """
        try:
            # Direct HTTP call to Management API; The auth0-python SDK does not expose this method (yet).
            mgmt_token = self.get_mgmt_api_token()
            url = f"https://{self.auth0_mgmt_domain}/api/v2/organizations/{org_id}/enabled_connections"
            body = {
                "connection_id": connection_id,
                "assign_membership_on_login": False,
            }
            headers = {
                "Authorization": f"Bearer {mgmt_token}",
                "Content-Type": "application/json",
            }
            resp = httpx.post(url, json=body, headers=headers, timeout=15)
            if resp.status_code in (200, 201, 409):  # 409 = already enabled
                return
            resp.raise_for_status()
        except Exception:
            logger.error(
                f"Error enabling connection {connection_id} for organization {org_id}",
                exc_info=True,
            )
            raise

    def get_username_password_connection_id(self) -> str:
        """Get the Username-Password-Authentication connection ID, caching it on first request."""
        if Auth0Service._cached_username_password_connection_id is None:
            # Cache the value on first request
            conn_id = self.get_connection_id_by_name("Username-Password-Authentication")
            if not conn_id:
                raise RuntimeError(
                    "Auth0 connection 'Username-Password-Authentication' not found. "
                    "This connection must be configured in Auth0 for the service to operate."
                )
            Auth0Service._cached_username_password_connection_id = conn_id
            logger.info("Cached Username-Password-Authentication connection ID")

        return Auth0Service._cached_username_password_connection_id

    def get_admin_role_id(self) -> str:
        """Get the Admin role ID, caching it on first request."""
        if Auth0Service._cached_admin_role_id is None:
            # Cache the value on first request
            role_id = self.get_role_id_by_name("Admin")
            if not role_id:
                raise RuntimeError(
                    "Auth0 role 'Admin' not found. "
                    "This role must be created in Auth0 for the service to operate."
                )
            Auth0Service._cached_admin_role_id = role_id
            logger.info("Cached Admin role ID")

        return Auth0Service._cached_admin_role_id

    def create_user(
        self,
        email: str,
        password: str,
        name: str | None = None,
        connection: str = "Username-Password-Authentication",
    ) -> dict[str, Any]:
        """Password must meet Auth0 password policy. User email is marked as verified."""
        try:
            mgmt_token = self.get_mgmt_api_token()
            url = f"https://{self.auth0_mgmt_domain}/api/v2/users"
            body = {
                "email": email,
                "password": password,
                "connection": connection,
                "email_verified": True,
            }
            if name:
                body["name"] = name
            headers = {
                "Authorization": f"Bearer {mgmt_token}",
                "Content-Type": "application/json",
            }

            resp = httpx.post(url, json=body, headers=headers, timeout=15)
            resp.raise_for_status()
            return resp.json()

        except Exception as e:
            logger.error(f"Error creating user '{email}': {e!s}")
            raise

    def add_organization_members(self, org_id: str, user_ids: list[str]) -> None:
        try:
            mgmt_token = self.get_mgmt_api_token()
            url = f"https://{self.auth0_mgmt_domain}/api/v2/organizations/{org_id}/members"
            body = {"members": user_ids}
            headers = {
                "Authorization": f"Bearer {mgmt_token}",
                "Content-Type": "application/json",
            }

            resp = httpx.post(url, json=body, headers=headers, timeout=15)
            if resp.status_code == 204:
                return
            resp.raise_for_status()

        except Exception as e:
            logger.error(f"Error adding members to organization {org_id}: {e!s}")
            raise

    def assign_organization_member_roles(
        self, org_id: str, user_id: str, role_ids: list[str]
    ) -> None:
        try:
            mgmt_token = self.get_mgmt_api_token()
            url = f"https://{self.auth0_mgmt_domain}/api/v2/organizations/{org_id}/members/{user_id}/roles"
            body = {"roles": role_ids}
            headers = {
                "Authorization": f"Bearer {mgmt_token}",
                "Content-Type": "application/json",
            }

            resp = httpx.post(url, json=body, headers=headers, timeout=15)
            if resp.status_code == 204:
                return
            resp.raise_for_status()

        except Exception as e:
            logger.error(
                f"Error assigning roles to user {user_id} in organization {org_id}: {e!s}"
            )
            raise
