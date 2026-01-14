# RBAC Mutation Logging Summary

## Overview

All RBAC mutation operations now emit structured audit logs with consistent format. These logs capture **who** performed **what action** on **which resources** within **which organization**.

---

## Log Format

All RBAC logs use positional format strings (matching `core.py` authorization logging):

```python
logger.info(
    "RBAC mutation: action=%s, user_id=%s, org_id=%s, org_name=%s, ...",
    "<action_type>",
    "<user_id>",
    "<organization_id>",
    "<organization_display_name>",
    # ... action-specific values
)
```

**Common Fields:**
| Field | Description |
|-------|-------------|
| `action` | Action identifier (e.g., `team.create`, `invitation.delete`) |
| `user_id` | Auth0 user ID of the person performing the action |
| `org_id` | Organization ID where the action occurred |
| `org_name` | Organization display name (human-readable) |

---

## Logged Actions by Category

### 1. Team Management
**File:** `backend/app/services/team_service.py`

| Action | Method | Additional Fields |
|--------|--------|-------------------|
| `team.create` | `create_team()` | `team_id`, `team_name`, `initial_members` |
| `team.update` | `update_team()` | `team_id`, `old_name`, `new_name` |
| `team.delete` | `delete_team()` | `team_id`, `team_name` |

---

### 2. Team Membership
**File:** `backend/app/services/team_member_service.py`

| Action | Method | Additional Fields |
|--------|--------|-------------------|
| `team.member.add` | `add_team_members()` | `team_id`, `added_members` (user_id, role) |
| `team.member.update` | `update_team_members()` | `team_id`, `changes` (user_id, old_role, new_role) |
| `team.member.remove` | `remove_team_members()` | `team_id`, `removed_user_ids` |

---

### 3. Team Source Access
**File:** `backend/app/services/source_access_service.py`

| Action | Method | Additional Fields |
|--------|--------|-------------------|
| `team.source.add` | `add_team_sources()` | `team_id`, `added_sources` (source_id, role) |
| `team.source.update` | `update_team_sources()` | `team_id`, `changes` (source_id, old_role, new_role) |
| `team.source.remove` | `remove_team_sources()` | `team_id`, `removed_source_ids` |

---

### 4. Source User Access (Direct Grants)
**File:** `backend/app/services/source_access_service.py`

| Action | Method | Additional Fields |
|--------|--------|-------------------|
| `source.user.add` | `add_source_users()` | `source_id`, `added_users` (user_id, role) |
| `source.user.update` | `update_source_users()` | `source_id`, `changes` (user_id, old_role, new_role) |
| `source.user.remove` | `remove_source_users()` | `source_id`, `removed_user_ids` |

---

### 5. Source Team Access
**File:** `backend/app/services/source_access_service.py`

| Action | Method | Additional Fields |
|--------|--------|-------------------|
| `source.team.add` | `add_source_teams()` | `source_id`, `added_teams` (team_id, role) |
| `source.team.update` | `update_source_teams()` | `source_id`, `changes` (team_id, old_role, new_role) |
| `source.team.remove` | `remove_source_teams()` | `source_id`, `removed_team_ids` |

---

### 6. User Team Assignments (Admin)
**File:** `backend/app/services/user_service.py`

| Action | Method | Additional Fields |
|--------|--------|-------------------|
| `user.team.add` | `add_user_teams()` | `target_user_id`, `teams` (team_id, role) |
| `user.team.update` | `update_user_teams()` | `target_user_id`, `changes` (team_id, old_role, new_role) |
| `user.team.remove` | `remove_user_teams()` | `target_user_id`, `removed_team_ids` |

---

### 7. User Source Access (Admin)
**File:** `backend/app/services/user_service.py`

| Action | Method | Additional Fields |
|--------|--------|-------------------|
| `user.source.add` | `add_user_sources()` | `target_user_id`, `sources` (source_id, role) |
| `user.source.update` | `update_user_sources()` | `target_user_id`, `changes` (source_id, old_role, new_role) |
| `user.source.remove` | `remove_user_sources()` | `target_user_id`, `removed_source_ids` |

---

### 8. Organization Members
**File:** `backend/app/services/organizations_service.py`

| Action | Method | Additional Fields |
|--------|--------|-------------------|
| `org.member.delete` | `delete_member()` | `removed_user_id`, `removed_user_role` |
| `org.member.role.update` | `update_member_role()` | `target_user_id`, `old_role`, `new_role` |
| `org.member.role.bulk_update` | `bulk_update_member_roles()` | `changes` (user_id, old_role, new_role) |

---

### 9. Organization Invitations
**File:** `packages/shared/shared/auth0/auth0_service.py`

| Action | Method | Additional Fields |
|--------|--------|-------------------|
| `invitation.create` | `create_invitation()` | `invitees` (email, role) |
| `invitation.delete` | `delete_invitation()` | `invitation_id` |

---

## Files Modified

| File | Methods Logged |
|------|----------------|
| `backend/app/services/team_service.py` | 3 |
| `backend/app/services/team_member_service.py` | 3 |
| `backend/app/services/source_access_service.py` | 9 |
| `backend/app/services/user_service.py` | 6 |
| `backend/app/services/organizations_service.py` | 3 |
| `packages/shared/shared/auth0/auth0_service.py` | 2 |
| **Total** | **26 methods** |

---

## Example Log Output

```
RBAC mutation: action=team.member.add, user_id=auth0|abc123, org_id=org_xyz789, org_name=Acme Corp, team_id=550e8400-e29b-41d4-a716-446655440000, added_members=[{'user_id': 'auth0|def456', 'role': 'viewer'}, {'user_id': 'auth0|ghi789', 'role': 'editor'}]
```

---

## What's NOT Logged

- **Read operations** (GET endpoints) - No mutation, no audit needed
- **Authorization decisions** - Already logged separately in `backend/app/authorization/core.py`
- **Failed operations** - Exceptions are logged with error level, not as RBAC mutations

---

## Querying Logs

Filter for all RBAC mutations:
```
message LIKE "RBAC mutation:%"
```

Filter by action type:
```
message LIKE "%action=team.member.add%"
```

Filter by actor:
```
message LIKE "%user_id=auth0|abc123%"
```

Filter by organization:
```
message LIKE "%org_id=org_xyz789%"
```
