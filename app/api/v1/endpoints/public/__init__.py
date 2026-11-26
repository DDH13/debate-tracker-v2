from fastapi import APIRouter

from app.api.v1.endpoints.public import debates, tournaments

# Page endpoints for the public site: one response per page, built by app/services/public.
# Separate from the console's resource routes; the middleware in app/main.py adds the
# Cache-Control header to everything under this prefix.
router = APIRouter(prefix="/public")
# Static paths (`/debates`, `/search`, ...) are declared before `/{id}` inside each module.
router.include_router(tournaments.router)
router.include_router(debates.router)
