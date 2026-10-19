"""Expose existing JSON routers below the versioned public API prefix."""

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute


def include_versioned_router(app: FastAPI, source: APIRouter) -> None:
    """Mount an API router at /api/v1 while keeping its original routes available."""
    prefix = source.prefix
    if not prefix.startswith("/api"):
        raise ValueError(f"Expected an /api router, got {prefix!r}")

    target = APIRouter(prefix="/api/v1" + prefix[4:])
    for route in source.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.path.startswith(prefix):
            raise ValueError(f"Route {route.path!r} is outside {prefix!r}")
        path = route.path[len(prefix):]
        target.add_api_route(
            path,
            route.endpoint,
            methods=route.methods,
            response_model=route.response_model,
            status_code=route.status_code,
            tags=route.tags,
            dependencies=route.dependencies,
            summary=route.summary,
            description=route.description,
            response_description=route.response_description,
            responses=route.responses,
            deprecated=route.deprecated,
            response_class=route.response_class,
            name="v1_" + route.name,
            operation_id="v1_" + route.unique_id,
            response_model_include=route.response_model_include,
            response_model_exclude=route.response_model_exclude,
            response_model_by_alias=route.response_model_by_alias,
            response_model_exclude_unset=route.response_model_exclude_unset,
            response_model_exclude_defaults=route.response_model_exclude_defaults,
            response_model_exclude_none=route.response_model_exclude_none,
            include_in_schema=route.include_in_schema,
            openapi_extra=route.openapi_extra,
        )
    app.include_router(target)


def include_legacy_router(app: FastAPI, source: APIRouter) -> None:
    """Keep old paths callable while publishing only the versioned contract."""
    for route in source.routes:
        if isinstance(route, APIRoute):
            route.include_in_schema = False
    app.include_router(source)
