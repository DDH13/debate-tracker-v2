from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import SessionDep
from app.services.public import search as search_service

router = APIRouter(tags=["public"])


@router.get("/search", response_model=search_service.SearchResults)
def global_search(
    session: SessionDep,
    q: str = Query(description="At least 2 characters after trimming"),
    limit: int = Query(default=5, ge=1, le=10, description="Hits per kind"),
) -> search_service.SearchResults:
    if len(q.strip()) < search_service.MIN_QUERY_LENGTH:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"q must be at least {search_service.MIN_QUERY_LENGTH} characters",
        )
    return search_service.search(session, q, limit)
