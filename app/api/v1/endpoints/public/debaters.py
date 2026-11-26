from fastapi import APIRouter

from app.api.deps import SessionDep, get_or_404
from app.models import Debater
from app.services.public import debater as debater_page

router = APIRouter(prefix="/debaters", tags=["public"])


@router.get("/{debater_id}", response_model=debater_page.PublicDebater)
def get_public_debater(debater_id: int, session: SessionDep) -> debater_page.PublicDebater:
    return debater_page.build_public_debater(session, get_or_404(session, Debater, debater_id))
