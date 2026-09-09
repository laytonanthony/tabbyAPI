"""Authenticated model catalogue for the Zeta desktop client."""

from __future__ import annotations

import hashlib
from typing import Iterable

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy.orm import Session

from open_webui.env import BYPASS_MODEL_ACCESS_CONTROL
from open_webui.models.access_grants import AccessGrants
from open_webui.models.groups import Groups
from open_webui.models.models import Models
from open_webui.models.users import UserModel
from open_webui.routers import openai
from open_webui.utils.auth import get_verified_user
from open_webui.utils.zeta_model_catalog import (
    SCHEMA_VERSION,
    build_catalogue_models,
    catalogue_revision,
    filter_authorised_models,
    generated_at,
    if_none_match_matches,
    is_catalogue_member,
    merge_catalogue_status,
    response_headers,
)


log = logger.bind(component="zeta_model_catalog")
router = APIRouter()


def _user_fingerprint(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:12]


def _live_ids(response: dict) -> set[str]:
    if not isinstance(response, dict):
        return set()
    data = response.get("data")
    if not isinstance(data, list):
        return set()
    return {
        model_id
        for model in data
        if isinstance(model, dict)
        and isinstance(model_id := model.get("id"), str)
        and model_id
    }


def _authorised_registry_models(
    *,
    user: UserModel,
    live_model_ids: Iterable[str],
    db: Session | None,
) -> tuple[list, set[str]]:
    registry_models = Models.get_all_models(db=db)
    registered_model_ids = {
        model.id for model in registry_models if isinstance(model.id, str)
    }
    candidates = [
        model
        for model in registry_models
        if is_catalogue_member(model, live_model_ids)
    ]

    # This mirrors the Responses endpoint: admins and installations that
    # explicitly bypass model ACLs may use every configured catalogue model.
    granted_ids: set[str] = set()
    if user.role != "admin" and not BYPASS_MODEL_ACCESS_CONTROL:
        group_ids = {
            group.id for group in Groups.get_groups_by_member_id(user.id, db=db)
        }
        candidate_ids = [model.id for model in candidates]
        granted_ids = AccessGrants.get_accessible_resource_ids(
            user_id=user.id,
            resource_type="model",
            resource_ids=candidate_ids,
            permission="read",
            user_group_ids=group_ids,
            db=db,
        )

    return (
        filter_authorised_models(
            candidates,
            user_id=user.id,
            user_role=user.role,
            bypass_model_access_control=BYPASS_MODEL_ACCESS_CONTROL,
            granted_model_ids=granted_ids,
        ),
        registered_model_ids,
    )


def _append_unregistered_live_models(
    models: list,
    *,
    live_model_ids: Iterable[str],
    registered_model_ids: set[str],
    user: UserModel,
) -> list:
    """Include routable live IDs when Responses itself permits unregistered IDs.

    Ordinary users still need a registry row because that is how the existing
    Responses access check establishes ownership/read grants. Admins and
    installations explicitly bypassing that check may use live provider IDs
    without a row, so the catalogue mirrors that behaviour with safe defaults.
    """

    if user.role != "admin" and not BYPASS_MODEL_ACCESS_CONTROL:
        return models
    existing_ids = set(registered_model_ids)
    result = list(models)
    for model_id in live_model_ids:
        if model_id in existing_ids:
            continue
        existing_ids.add(model_id)
        result.append(
            {
                "id": model_id,
                "name": model_id,
                "user_id": None,
                "base_model_id": None,
                "is_active": True,
                "meta": {},
            }
        )
    return result


async def catalogue_for_user(
    request: Request,
    user: UserModel,
    *,
    db: Session | None = None,
    live_model_ids: Iterable[str] | None = None,
) -> list[dict]:
    """Build the current user's catalogue without exposing registry objects."""

    if live_model_ids is None:
        discovered = await openai.get_all_models(request, user=user)
        live_model_ids = _live_ids(discovered)
    else:
        live_model_ids = set(live_model_ids)

    registry_models, registered_model_ids = _authorised_registry_models(
        user=user,
        live_model_ids=live_model_ids,
        db=db,
    )
    registry_models = _append_unregistered_live_models(
        registry_models,
        live_model_ids=live_model_ids,
        registered_model_ids=registered_model_ids,
        user=user,
    )
    return build_catalogue_models(
        registry_models,
        live_model_ids=live_model_ids,
        model_order_list=request.app.state.config.MODEL_ORDER_LIST or [],
    )


@router.get("/catalog")
async def get_model_catalogue(
    request: Request,
    user: UserModel = Depends(get_verified_user),
):
    """Return every Responses model the authenticated user may select."""

    user_key = _user_fingerprint(user.id)
    try:
        models = await catalogue_for_user(request, user)
        revision = catalogue_revision(models)
        headers = response_headers(revision)

        if if_none_match_matches(request.headers.get("if-none-match"), revision):
            log.bind(
                event="zeta_model_catalog_not_modified",
                user_fingerprint=user_key,
                revision=revision[:12],
                model_count=len(models),
            ).info("Zeta model catalogue not modified")
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)

        log.bind(
            event="zeta_model_catalog_served",
            user_fingerprint=user_key,
            revision=revision[:12],
            model_count=len(models),
        ).info("Zeta model catalogue served")
        return JSONResponse(
            content={
                "schema_version": SCHEMA_VERSION,
                "revision": revision,
                "generated_at": generated_at(),
                "models": models,
            },
            headers=headers,
        )
    except Exception as exc:
        log.bind(
            event="zeta_model_catalog_failed",
            user_fingerprint=user_key,
            error_type=type(exc).__name__,
        ).error("Zeta model catalogue failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to build model catalogue",
        ) from None
