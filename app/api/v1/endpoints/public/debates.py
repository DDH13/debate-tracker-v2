from typing import Literal

from fastapi import APIRouter, Query

from app.api.deps import SessionDep, get_or_404
from app.models import Debate
from app.services.public import debates as debate_service

router = APIRouter(prefix="/debates", tags=["public"])


# Static before `/{debate_id}`; the list is the root of this router.
@router.get("", response_model=debate_service.DebateList)
def explore_debates(
    session: SessionDep,
    tournament_id: int | None = None,
    round_id: int | None = None,
    team_id: int | None = None,
    debater_id: int | None = None,
    judge_id: int | None = None,
    institution_id: int | None = None,
    stage: Literal["prelim", "elim"] | None = None,
    q: str | None = Query(default=None, description="Motion text contains"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
) -> debate_service.DebateList:
    return debate_service.list_debates(
        session,
        tournament_id=tournament_id,
        round_id=round_id,
        team_id=team_id,
        debater_id=debater_id,
        judge_id=judge_id,
        institution_id=institution_id,
        stage=stage,
        q=q,
        offset=offset,
        limit=limit,
    )


@router.get("/{debate_id}", response_model=debate_service.PublicDebate)
def get_public_debate(debate_id: int, session: SessionDep) -> debate_service.PublicDebate:
    return debate_service.build_public_debate(session, get_or_404(session, Debate, debate_id))
