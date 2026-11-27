"""SCIM 2.0 provisioning (RFC 7643/7644): Users and Groups, for Entra ID, Okta and similar.

Mounted at `/scim/v2`. Authenticated with one bearer token held by the identity provider
(`SCIM_BEARER_TOKEN`); SCIM is off when that variable is unset. Deleting or deactivating a user
disables sign-in and revokes every token immediately. Group membership drives directory-managed
roles through the deployment's `group_roles` mapping.
"""

from __future__ import annotations

import hmac
import os
import re
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentic_sdlc.api.deps import DB
from agentic_sdlc.auth.directory import deactivate, new_user, sync_roles
from agentic_sdlc.core.errors import PolicyViolation
from agentic_sdlc.core.settings import get_settings
from agentic_sdlc.orm import DirectoryGroup, User

router = APIRouter(prefix="/scim/v2", tags=["scim"])

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
MEDIA = "application/scim+json"


class ScimError(Exception):
    def __init__(self, status: int, detail: str, scim_type: str | None = None) -> None:
        super().__init__(detail)
        self.status, self.detail, self.scim_type = status, detail, scim_type


def scim_response(body: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type=MEDIA)


def error_response(exc: ScimError) -> JSONResponse:
    body: dict[str, Any] = {"schemas": [ERROR_SCHEMA], "status": str(exc.status), "detail": exc.detail}
    if exc.scim_type:
        body["scimType"] = exc.scim_type
    return scim_response(body, exc.status)


def require_scim(authorization: Annotated[str | None, Header()] = None) -> None:
    expected = os.environ.get(get_settings().scim_token_env)
    if not expected:
        raise ScimError(404, "SCIM provisioning is not enabled")
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not given or not hmac.compare_digest(given.encode(), expected.encode()):
        raise ScimError(401, "invalid SCIM credentials")


Auth = Annotated[None, Depends(require_scim)]


def _base(request: Request) -> str:
    return get_settings().public_base_url.rstrip("/") + "/scim/v2"


def _user_out(u: User, request: Request, db: Session) -> dict[str, Any]:
    groups = db.scalars(select(DirectoryGroup).where(DirectoryGroup.members.contains([str(u.id)]))).all()
    given, _, family = u.display_name.partition(" ")
    return {
        "schemas": [USER_SCHEMA],
        "id": str(u.id),
        "externalId": u.external_id,
        "userName": u.email,
        "displayName": u.display_name,
        "name": {"formatted": u.display_name, "givenName": given, "familyName": family},
        "emails": [{"value": u.email, "primary": True, "type": "work"}],
        "active": u.is_active,
        "groups": [{"value": str(g.id), "display": g.display_name} for g in groups],
        "meta": {
            "resourceType": "User",
            "created": u.created_at.isoformat(),
            "lastModified": u.updated_at.isoformat(),
            "location": f"{_base(request)}/Users/{u.id}",
        },
    }


def _group_out(g: DirectoryGroup, request: Request, db: Session) -> dict[str, Any]:
    ids = [uuid.UUID(m) for m in g.members]
    users = {str(u.id): u for u in db.scalars(select(User).where(User.id.in_(ids)))} if ids else {}
    return {
        "schemas": [GROUP_SCHEMA],
        "id": str(g.id),
        "externalId": g.external_id,
        "displayName": g.display_name,
        "members": [{"value": m, "display": users[m].email} for m in g.members if m in users],
        "meta": {
            "resourceType": "Group",
            "created": g.created_at.isoformat(),
            "lastModified": g.updated_at.isoformat(),
            "location": f"{_base(request)}/Groups/{g.id}",
        },
    }


_FILTER = re.compile(r'^\s*(\w+(?:\.\w+)?)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$', re.I)


def _parse_filter(f: str | None, allowed: set[str]) -> tuple[str, str] | None:
    if not f:
        return None
    m = _FILTER.match(f)
    if not m or m.group(1) not in allowed:
        raise ScimError(400, f"unsupported filter; supported: {sorted(allowed)} with eq", "invalidFilter")
    return m.group(1), m.group(2).replace('\\"', '"')


def _page(items: list[dict[str, Any]], total: int, start: int) -> dict[str, Any]:
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": total,
        "startIndex": start,
        "itemsPerPage": len(items),
        "Resources": items,
    }


def _bounds(start_index: int, count: int) -> tuple[int, int]:
    return max(1, start_index), max(0, min(count, 200))


def _user(db: Session, user_id: str) -> User:
    try:
        u = db.get(User, uuid.UUID(user_id))
    except ValueError:
        u = None
    if u is None or u.is_service_account:
        raise ScimError(404, "user not found")
    return u


def _group(db: Session, group_id: str) -> DirectoryGroup:
    try:
        g = db.get(DirectoryGroup, uuid.UUID(group_id))
    except ValueError:
        g = None
    if g is None:
        raise ScimError(404, "group not found")
    return g


def _email_of(body: dict[str, Any]) -> str:
    emails = body.get("emails") or []
    primary = next((e for e in emails if isinstance(e, dict) and e.get("primary")), None)
    value = body.get("userName") or (primary or (emails[0] if emails else {})).get("value")
    if not isinstance(value, str) or "@" not in value:
        raise ScimError(400, "userName must be an email address", "invalidValue")
    return value.strip().lower()


def _display_of(body: dict[str, Any], fallback: str) -> str:
    if isinstance(body.get("displayName"), str) and body["displayName"].strip():
        return str(body["displayName"]).strip()[:200]
    name = body.get("name") or {}
    joined = " ".join(x for x in (name.get("givenName"), name.get("familyName")) if isinstance(x, str) and x)
    return (name.get("formatted") or joined or fallback)[:200]


def _set_active(db: Session, u: User, active: Any) -> None:
    flag = active if isinstance(active, bool) else str(active).lower() == "true"
    if not flag and u.is_active:
        deactivate(db, u, actor="scim")
    elif flag:
        u.is_active = True


def _refresh_groups(db: Session, user_ids: set[str]) -> None:
    """Recompute each affected user's group names from SCIM groups, then their managed roles."""
    for uid in user_ids:
        try:
            u = db.get(User, uuid.UUID(uid))
        except ValueError:
            continue
        if u is None:
            continue
        names = db.scalars(
            select(DirectoryGroup.display_name).where(DirectoryGroup.members.contains([uid]))
        ).all()
        u.groups = sorted(names)
        u.scim_managed = True
        sync_roles(db, u, actor="scim")


# --- discovery -------------------------------------------------------------------------------------------


@router.get("/ServiceProviderConfig")
def service_provider_config(_: Auth) -> JSONResponse:
    return scim_response(
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": 200},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {"type": "oauthbearertoken", "name": "Bearer token", "description": "SCIM_BEARER_TOKEN"}
            ],
        }
    )


@router.get("/ResourceTypes")
def resource_types(_: Auth) -> JSONResponse:
    return scim_response(
        _page(
            [
                {
                    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                    "id": "User",
                    "name": "User",
                    "endpoint": "/Users",
                    "schema": USER_SCHEMA,
                },
                {
                    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                    "id": "Group",
                    "name": "Group",
                    "endpoint": "/Groups",
                    "schema": GROUP_SCHEMA,
                },
            ],
            2,
            1,
        )
    )


# --- users -----------------------------------------------------------------------------------------------


@router.get("/Users")
def list_users(
    request: Request,
    db: DB,
    _: Auth,
    filter: str | None = None,
    startIndex: int = 1,
    count: int = 100,
) -> JSONResponse:
    start, n = _bounds(startIndex, count)
    stmt = select(User).where(User.is_service_account.is_(False))
    f = _parse_filter(filter, {"userName", "externalId", "emails.value"})
    if f:
        attr, value = f
        stmt = (
            stmt.where(User.external_id == value)
            if attr == "externalId"
            else stmt.where(func.lower(User.email) == value.lower())
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    users = db.scalars(stmt.order_by(User.created_at).offset(start - 1).limit(n)).all()
    return scim_response(_page([_user_out(u, request, db) for u in users], total, start))


@router.post("/Users")
async def create_user(request: Request, db: DB, _: Auth) -> JSONResponse:
    body = await request.json()
    email = _email_of(body)
    existing = db.scalar(select(User).where(func.lower(User.email) == email))
    if existing is not None and (existing.scim_managed or existing.is_service_account):
        raise ScimError(409, "a user with this userName already exists", "uniqueness")
    # An existing (e.g. pre-SSO) account with this email is taken over by the directory.
    try:
        u = existing or new_user(db, email=email, display_name=_display_of(body, email))
    except PolicyViolation as exc:
        raise ScimError(403, exc.message) from None
    u.display_name = _display_of(body, u.display_name)
    u.external_id = body.get("externalId")
    u.scim_managed = True
    if "active" in body:
        _set_active(db, u, body["active"])
    db.commit()
    return scim_response(_user_out(u, request, db), 201)


@router.get("/Users/{user_id}")
def get_user(user_id: str, request: Request, db: DB, _: Auth) -> JSONResponse:
    return scim_response(_user_out(_user(db, user_id), request, db))


@router.put("/Users/{user_id}")
async def replace_user(user_id: str, request: Request, db: DB, _: Auth) -> JSONResponse:
    u = _user(db, user_id)
    body = await request.json()
    email = _email_of(body)
    clash = db.scalar(select(User).where(func.lower(User.email) == email, User.id != u.id))
    if clash is not None:
        raise ScimError(409, "another user has this userName", "uniqueness")
    u.email = email
    u.display_name = _display_of(body, email)
    u.external_id = body.get("externalId", u.external_id)
    u.scim_managed = True
    _set_active(db, u, body.get("active", True))
    db.commit()
    return scim_response(_user_out(u, request, db))


@router.patch("/Users/{user_id}")
async def patch_user(user_id: str, request: Request, db: DB, _: Auth) -> JSONResponse:
    u = _user(db, user_id)
    body = await request.json()
    if PATCH_SCHEMA not in body.get("schemas", []):
        raise ScimError(400, "expected a PatchOp request", "invalidSyntax")
    for op in body.get("Operations", []):
        kind = str(op.get("op", "")).lower()
        path = op.get("path")
        value = op.get("value")
        if kind not in ("replace", "add", "remove"):
            raise ScimError(400, f"unsupported op {kind}", "invalidSyntax")
        changes = {path: value} if path else (value if isinstance(value, dict) else {})
        for attr, v in changes.items():
            a = str(attr)
            if a == "active":
                _set_active(db, u, v if kind != "remove" else False)
            elif a in ("displayName", "name.formatted"):
                u.display_name = str(v)[:200]
            elif a == "userName" or a.startswith("emails"):
                email = (v[0].get("value") if isinstance(v, list) and v else v) or ""
                if isinstance(email, str) and "@" in email:
                    u.email = email.lower()
            elif a == "externalId":
                u.external_id = None if kind == "remove" else str(v)
            elif a in (
                "name.givenName",
                "name.familyName",
                "name",
                "title",
                "preferredLanguage",
            ) or a.startswith("urn:"):
                continue  # accepted, not stored
            else:
                raise ScimError(400, f"unsupported attribute {a}", "invalidPath")
    u.scim_managed = True
    db.commit()
    return scim_response(_user_out(u, request, db))


@router.delete("/Users/{user_id}", status_code=204)
def delete_user(user_id: str, db: DB, _: Auth) -> Response:
    u = _user(db, user_id)
    # Users are never hard-deleted: approvals and audit records must keep their author.
    deactivate(db, u, actor="scim")
    for g in db.scalars(select(DirectoryGroup).where(DirectoryGroup.members.contains([str(u.id)]))):
        g.members = [m for m in g.members if m != str(u.id)]
    _refresh_groups(db, {str(u.id)})
    db.commit()
    return Response(status_code=204)


# --- groups ----------------------------------------------------------------------------------------------


def _member_ids(db: Session, members: Any) -> list[str]:
    out: list[str] = []
    for m in members or []:
        value = m.get("value") if isinstance(m, dict) else None
        if not isinstance(value, str):
            raise ScimError(400, "each member needs a value", "invalidValue")
        _user(db, value)
        if value not in out:
            out.append(value)
    return out


@router.get("/Groups")
def list_groups(
    request: Request,
    db: DB,
    _: Auth,
    filter: str | None = None,
    startIndex: int = 1,
    count: int = 100,
) -> JSONResponse:
    start, n = _bounds(startIndex, count)
    stmt = select(DirectoryGroup)
    f = _parse_filter(filter, {"displayName", "externalId"})
    if f:
        attr, value = f
        stmt = stmt.where(
            DirectoryGroup.display_name == value
            if attr == "displayName"
            else DirectoryGroup.external_id == value
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    groups = db.scalars(stmt.order_by(DirectoryGroup.created_at).offset(start - 1).limit(n)).all()
    return scim_response(_page([_group_out(g, request, db) for g in groups], total, start))


@router.post("/Groups")
async def create_group(request: Request, db: DB, _: Auth) -> JSONResponse:
    body = await request.json()
    name = body.get("displayName")
    if not isinstance(name, str) or not name.strip():
        raise ScimError(400, "displayName is required", "invalidValue")
    if db.scalar(select(DirectoryGroup).where(DirectoryGroup.display_name == name)) is not None:
        raise ScimError(409, "a group with this displayName exists", "uniqueness")
    g = DirectoryGroup(
        id=uuid.uuid4(),
        display_name=name.strip()[:200],
        external_id=body.get("externalId"),
        members=_member_ids(db, body.get("members")),
    )
    db.add(g)
    db.flush()
    _refresh_groups(db, set(g.members))
    db.commit()
    return scim_response(_group_out(g, request, db), 201)


@router.get("/Groups/{group_id}")
def get_group(group_id: str, request: Request, db: DB, _: Auth) -> JSONResponse:
    return scim_response(_group_out(_group(db, group_id), request, db))


@router.put("/Groups/{group_id}")
async def replace_group(group_id: str, request: Request, db: DB, _: Auth) -> JSONResponse:
    g = _group(db, group_id)
    body = await request.json()
    before = set(g.members)
    g.display_name = str(body.get("displayName") or g.display_name)[:200]
    g.external_id = body.get("externalId", g.external_id)
    g.members = _member_ids(db, body.get("members"))
    db.flush()
    _refresh_groups(db, before | set(g.members))
    db.commit()
    return scim_response(_group_out(g, request, db))


_MEMBER_PATH = re.compile(r'^members\[value eq "([^"]+)"\]$')


@router.patch("/Groups/{group_id}")
async def patch_group(group_id: str, request: Request, db: DB, _: Auth) -> JSONResponse:
    g = _group(db, group_id)
    body = await request.json()
    if PATCH_SCHEMA not in body.get("schemas", []):
        raise ScimError(400, "expected a PatchOp request", "invalidSyntax")
    before = set(g.members)
    members = list(g.members)
    for op in body.get("Operations", []):
        kind = str(op.get("op", "")).lower()
        path = op.get("path") or ""
        value = op.get("value")
        if path == "members" or (not path and isinstance(value, dict) and "members" in value):
            items = value if path else value["members"]
            ids = _member_ids(db, items if isinstance(items, list) else [items])
            if kind == "add":
                members += [i for i in ids if i not in members]
            elif kind == "replace":
                members = ids
            elif kind == "remove":
                members = [] if not ids else [m for m in members if m not in ids]
        elif (m := _MEMBER_PATH.match(path)) and kind == "remove":
            members = [x for x in members if x != m.group(1)]
        elif path == "displayName" or (not path and isinstance(value, dict) and "displayName" in value):
            g.display_name = str(value if path else value["displayName"])[:200]
        elif path == "externalId":
            g.external_id = None if kind == "remove" else str(value)
        else:
            raise ScimError(400, f"unsupported patch path {path!r}", "invalidPath")
    g.members = members
    db.flush()
    _refresh_groups(db, before | set(members))
    db.commit()
    return scim_response(_group_out(g, request, db))


@router.delete("/Groups/{group_id}", status_code=204)
def delete_group(group_id: str, db: DB, _: Auth) -> Response:
    g = _group(db, group_id)
    affected = set(g.members)
    db.delete(g)
    db.flush()
    _refresh_groups(db, affected)
    db.commit()
    return Response(status_code=204)
