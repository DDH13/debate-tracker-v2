from fastapi import APIRouter, Query

from app.api.deps import SessionDep, get_or_404
from app.models import Institution
from app.services.public_site import institution as institution_page
from app.services.public_site import lists

router = APIRouter(prefix="/institutions", tags=["public"])


# Static before `/{institution_id}`; the list is the root of this router.
@router.get("", response_model=lists.InstitutionList)
def list_public_institutions(
    session: SessionDep,
    q: str | None = None,
    min_teams: int = Query(default=0, ge=0),
    sort: lists.InstitutionSort = "titles",
    order: lists.Order = "desc",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
) -> lists.InstitutionList:
    return lists.list_institutions(
        session, q=q, min_teams=min_teams, sort=sort, order=order, offset=offset, limit=limit
    )


@router.get("/{institution_id}", response_model=institution_page.PublicInstitution)
def get_public_institution(institution_id: int, session: SessionDep) -> institution_page.PublicInstitution:
    return institution_page.build_public_institution(session, get_or_404(session, Institution, institution_id))
