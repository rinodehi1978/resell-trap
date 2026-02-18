"""Aggregate all API routers."""

from fastapi import APIRouter

from . import items, search, status, system

api_router = APIRouter()
api_router.include_router(items.router)
api_router.include_router(search.router)
api_router.include_router(status.router)
api_router.include_router(system.router)
