"""Aggregate all API routers."""

from fastapi import APIRouter

from . import amazon, discovery, items, keepa, keywords, search, status, system, templates

api_router = APIRouter()
api_router.include_router(items.router)
api_router.include_router(search.router)
api_router.include_router(status.router)
api_router.include_router(system.router)
api_router.include_router(amazon.router)
api_router.include_router(keepa.router)
api_router.include_router(keywords.router)
api_router.include_router(discovery.router)
api_router.include_router(templates.router)
