from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse


@dataclass(frozen=True)
class NavigationDependencies:
    build_dashboard_shell_context: Callable[..., dict[str, Any]]
    ensure_dashboard_snapshot_refresh: Callable[[Request], Awaitable[None]]
    peek_status_wall_payload: Callable[[], Optional[dict[str, Any]]]
    build_status_wall_payload: Callable[..., dict[str, Any]]
    list_status_wall_monitors: Callable[[bool], list[dict[str, Any]]]
    ensure_status_wall_refresh: Callable[[], Awaitable[None]]
    get_settings: Callable[[], dict[str, Any]]
    get_app_version_display: Callable[[], str]
    build_settings_context: Callable[[Request], dict[str, Any]]
    build_incidents_shell_context: Callable[[Request], dict[str, Any]]
    build_changelog_context: Callable[[Request], dict[str, Any]]
    render_template: Callable[[Request, str, dict[str, Any]], HTMLResponse]
    logger: logging.Logger


router = APIRouter(tags=["navigation"])
_dependencies: Optional[NavigationDependencies] = None


def configure_navigation_routes(dependencies: NavigationDependencies) -> None:
    global _dependencies
    _dependencies = dependencies


def _deps() -> NavigationDependencies:
    if _dependencies is None:
        raise RuntimeError("Navigation router is not configured")
    return _dependencies


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    deps = _deps()
    context = await asyncio.to_thread(deps.build_dashboard_shell_context, request, cached_only=True)
    await deps.ensure_dashboard_snapshot_refresh(request)
    return await asyncio.to_thread(deps.render_template, request, "index.html", context)


@router.get("/wall", response_class=HTMLResponse)
async def status_wall(request: Request) -> HTMLResponse:
    deps = _deps()
    payload = deps.peek_status_wall_payload()
    if payload is None:
        try:
            payload = await asyncio.wait_for(asyncio.to_thread(deps.build_status_wall_payload), timeout=8.0)
        except asyncio.TimeoutError:
            deps.logger.warning("status_wall_initial_build_timeout")
            monitors = await asyncio.to_thread(deps.list_status_wall_monitors, False)
            payload = deps.build_status_wall_payload(monitors)
            payload["details_pending"] = True
            await deps.ensure_status_wall_refresh()
    context = {
        "request": request,
        "settings": await asyncio.to_thread(deps.get_settings),
        "payload": payload,
        "app_version": deps.get_app_version_display(),
    }
    return await asyncio.to_thread(deps.render_template, request, "status_wall.html", context)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    deps = _deps()
    context = await asyncio.to_thread(deps.build_settings_context, request)
    return await asyncio.to_thread(deps.render_template, request, "settings.html", context)


@router.get("/incidents", response_class=HTMLResponse)
async def incidents_page(request: Request) -> HTMLResponse:
    deps = _deps()
    context = await asyncio.to_thread(deps.build_incidents_shell_context, request)
    return await asyncio.to_thread(deps.render_template, request, "incidents.html", context)


@router.get("/changelog", response_class=HTMLResponse)
async def changelog_page(request: Request) -> HTMLResponse:
    deps = _deps()
    context = await asyncio.to_thread(deps.build_changelog_context, request)
    return await asyncio.to_thread(deps.render_template, request, "changelog.html", context)
