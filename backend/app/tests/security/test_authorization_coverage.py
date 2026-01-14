"""
Tests that verify EVERY endpoint has authorization or is exempt.
Fails the build if any endpoint is missing authorization checks.
DOES NOT check correctness of authorization checks.
"""

import inspect
from typing import Any

import pytest
from fastapi import APIRouter

# Endpoints that are intentionally unprotected (no authentication required)
# Note: These paths exist at BOTH /studio/v1/* and /api/v1/* (dual registration in main.py)
ALLOWED_UNPROTECTED = {
    # Studio v1 paths
    "/studio/v1/healthcheck",
    "/studio/v1/git-provider/github/webhook",  # GitHub webhook (signature verification)
    "/studio/v1/git-provider/github/callback",  # OAuth callback
    "/studio/v1/git-provider/app/webhook",  # GitHub App webhook
    "/studio/v1/git-provider/app/callback",  # GitHub App OAuth callback
    "/studio/v1/git-provider/app/{application_id}/installations/{installation_id}/webhook",  # Get webhook info
    "/studio/v1/onboarding",
    "/studio/v1/subscription",
    "/studio/v1/signup",
    "/studio/v1/sandbox",  # dev only (not in production)
    # API v1 paths (same routes, different prefix - for webhooks/legacy clients)
    "/api/v1/healthcheck",
    "/api/v1/git-provider/github/webhook",
    "/api/v1/git-provider/github/callback",
    "/api/v1/git-provider/app/webhook",
    "/api/v1/git-provider/app/callback",
    "/api/v1/git-provider/app/{application_id}/installations/{installation_id}/webhook",
    "/api/v1/onboarding",
    "/api/v1/subscription",
    "/api/v1/signup",
    "/api/v1/sandbox",
}

# GraphQL endpoints with context-level authentication
# These use context_getter with require_jwt instead of route-level auth
GRAPHQL_ENDPOINTS = {
    "/studio/v1/graphql",  # Uses context_getter=get_context which requires JWT
}

# Self-service/utility endpoints that only need authentication (UserToken)
SELF_SERVICE_ENDPOINTS = {
    "/studio/v1/user/password",  # Changes user's own Auth0 password
    "/studio/v1/user/organizations",  # Lists user's Auth0 orgs
    "/studio/v1/content/export-rst",  # Utility: converts MD to RST (stateless)
}


def extract_all_routes(router: APIRouter, prefix: str = "") -> list[dict]:
    routes = []
    for route in router.routes:
        if hasattr(route, "path"):
            full_path = prefix + route.path
            methods = route.methods if hasattr(route, "methods") else set()
            routes.append(
                {
                    "path": full_path,
                    "methods": sorted(methods),
                    "endpoint": route.endpoint,
                    "name": route.name if hasattr(route, "name") else None,
                }
            )
        if hasattr(route, "routes"):  # Nested router
            routes.extend(extract_all_routes(route, prefix + route.path))
    return routes


def has_user_token_dependency(endpoint_func: Any) -> bool:
    """Check if endpoint has UserToken, ApiKeyToken, or M2MToken in signature."""
    try:
        source = inspect.getsource(endpoint_func)

        # Check for token types in the source code
        # This works better than type hints for Annotated types
        auth_patterns = ["UserToken", "ApiKeyToken", "M2MToken"]
        return any(pattern in source for pattern in auth_patterns)
    except (OSError, TypeError):
        return False


def calls_authorization_function(endpoint_func: Any) -> bool:
    """A crude way to check if the endpoint does authz"""
    try:
        source = inspect.getsource(endpoint_func)
        auth_patterns = [
            "enforce_asset_action",
            "enforce_org_action",
            "enforce_org_actions",
            "enforce_team_action",
            "enforce_org_membership",
            "enforce_super_admin",
            "enforce_any_source_admin",
            "enforce_any_team_admin",
            "check_asset_action",
            "check_org_action",
            "check_team_action",
            "check_org_membership",
            "check_super_admin",
            "check_source_admin",
            "check_team_admin",
            # Query filters that enforce authorization
            "primary_asset_grant_filter",
            "page_asset_grant_filter",
            "page_content_grant_filter",
            "content_grant_filter",
            "page_source_authorization_filter",
            "team_grant_filter",
        ]
        return any(pattern in source for pattern in auth_patterns)
    except (OSError, TypeError):
        return False


def is_path_allowed_unprotected(path: str) -> bool:
    return any(path.startswith(allowed) for allowed in ALLOWED_UNPROTECTED)


def is_graphql_endpoint(path: str) -> bool:
    return any(path.startswith(gql) for gql in GRAPHQL_ENDPOINTS)


def is_self_service_endpoint(path: str) -> bool:
    return any(path.startswith(svc) for svc in SELF_SERVICE_ENDPOINTS)


@pytest.fixture(scope="module")
def all_studio_routes() -> list[dict[str, Any]]:
    from app.api.studio_router import studio_router

    return extract_all_routes(studio_router, prefix="/studio/v1")


@pytest.fixture(scope="module")
def all_unprotected_routes() -> list[dict[str, Any]]:
    """
    Collect all routes from unprotected_router.

    Note: unprotected_router is mounted at BOTH /studio/v1 and /api/v1 in main.py,
    so we extract routes for both prefixes.
    """
    from app.api.unprotected_router import unprotected_router

    # Unprotected router is mounted at both /studio/v1 and /api/v1
    studio_routes = extract_all_routes(unprotected_router, prefix="/studio/v1")
    api_routes = extract_all_routes(unprotected_router, prefix="/api/v1")

    return studio_routes + api_routes


def get_testable_routes(all_routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    testable = []
    for route in all_routes:
        path = route["path"]

        # Skip if explicitly allowed to be unprotected
        if is_path_allowed_unprotected(path):
            continue

        # Skip GraphQL endpoints (have context-level auth)
        if is_graphql_endpoint(path):
            continue

        # Skip OPTIONS methods (CORS preflight)
        if route["methods"] == ["OPTIONS"]:
            continue

        testable.append(route)

    return testable


@pytest.fixture(scope="module")
def protected_routes(all_studio_routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """all routes that should have authentication and authorization."""
    return get_testable_routes(all_studio_routes)


def pytest_generate_tests(metafunc: Any) -> None:
    """Parametrize tests with individual routes."""
    if "route" in metafunc.fixturenames and "protected_routes" in metafunc.fixturenames:
        from app.api.studio_router import studio_router

        all_routes = extract_all_routes(studio_router, prefix="/studio/v1")
        routes = get_testable_routes(all_routes)

        # Create test IDs from route path and methods
        ids = [f"{','.join(r['methods'])} {r['path']}" for r in routes]
        metafunc.parametrize("route", routes, ids=ids)

    if (
        "unprotected_route" in metafunc.fixturenames
        and "all_unprotected_routes" in metafunc.fixturenames
    ):
        from app.api.unprotected_router import unprotected_router

        # Unprotected router is mounted at both /studio/v1 and /api/v1
        studio_routes = extract_all_routes(unprotected_router, prefix="/studio/v1")
        api_routes = extract_all_routes(unprotected_router, prefix="/api/v1")
        routes = studio_routes + api_routes

        # Create test IDs from route path and methods
        ids = [f"{','.join(r['methods'])} {r['path']}" for r in routes]
        metafunc.parametrize("unprotected_route", routes, ids=ids)


def test_endpoint_has_authentication_and_authorization(
    route: dict[str, Any], protected_routes: list[dict[str, Any]]
) -> None:
    """
    Verifies that every protected endpoint either:
    1. Has UserToken/ApiKeyToken/M2MToken dependency (authentication), AND
    2. Calls an enforce_* or check_* function (authorization)
    OR is a self-service endpoint (only needs authentication)
    """
    path = route["path"]
    endpoint = route["endpoint"]

    # Self-service endpoints only need authentication
    if is_self_service_endpoint(path):
        has_auth = has_user_token_dependency(endpoint)
        assert has_auth, (
            "Self-service endpoint missing authentication. "
            "Add UserToken parameter to the endpoint."
        )
        return

    # Regular protected endpoints need both authentication and authorization
    has_auth = has_user_token_dependency(endpoint)
    has_authz = calls_authorization_function(endpoint)

    if not has_auth:
        pytest.fail(
            "Missing authentication (no UserToken/ApiKeyToken/M2MToken). "
            "Add one of these parameters to the endpoint function."
        )

    if not has_authz:
        pytest.fail(
            "Missing authorization check (no enforce_*/check_* call). "
            "Add authorization checks like enforce_asset_action(), enforce_org_action(), "
            "or use query filters like primary_asset_grant_filter()."
        )


def test_unprotected_endpoint_is_safe(
    unprotected_route: dict[str, Any], all_unprotected_routes: list[dict[str, Any]]
) -> None:
    """Verify unprotected router only contains expected safe endpoints."""
    path = unprotected_route["path"]

    # TODO dont just skip gql. we should check authz if possible

    # Known issue: git-provider router mixes protected and unprotected routes
    # This should be refactored but is marked as xfail for now
    if (
        path.startswith(("/studio/v1/git-provider/app", "/api/v1/git-provider/app"))
        and "/webhook" not in path
    ):
        pytest.xfail(
            "git-provider router mixes protected (UserToken) and unprotected (webhook) routes. "
            "This should be refactored into separate routers but is safe for now since "
            "the protected routes DO have UserToken + enforce_org_action."
        )

    assert is_path_allowed_unprotected(path), (
        f"Unexpected unprotected endpoint: {path}. "
        f"This endpoint is in unprotected_router but not in ALLOWED_UNPROTECTED. "
        f"Either add authentication to this endpoint and move it to studio_router, "
        f"or add it to ALLOWED_UNPROTECTED if it's genuinely safe (webhooks, OAuth callbacks, etc.)."
    )
