from fastapi import APIRouter, HTTPException, Query, status
from sqlmodel import select

from app.api.deps import SessionDep, get_or_404
from app.models import Debate, DebateCreate, DebatePublic, DebateUpdate, Round, Team

router = APIRouter(tags=["debates"])


def _validate_teams(session: SessionDep, round_: Round, prop_team_id: int, opp_team_id: int) -> None:
    if prop_team_id == opp_team_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="prop_team_id and opp_team_id must be different",
        )
    prop_team = get_or_404(session, Team, prop_team_id)
    opp_team = get_or_404(session, Team, opp_team_id)
    if prop_team.tournament_id != round_.tournament_id or opp_team.tournament_id != round_.tournament_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Both teams must belong to the same tournament as the round",
        )


@router.post(
    "/rounds/{round_id}/debates",
    response_model=DebatePublic,
    status_code=status.HTTP_201_CREATED,
)
def create_debate(round_id: int, debate_in: DebateCreate, session: SessionDep) -> Debate:
    round_ = get_or_404(session, Round, round_id)
    _validate_teams(session, round_, debate_in.prop_team_id, debate_in.opp_team_id)
    debate = Debate.model_validate(debate_in, update={"round_id": round_id})
    session.add(debate)
    session.commit()
    session.refresh(debate)
    return debate


@router.get("/rounds/{round_id}/debates", response_model=list[DebatePublic])
def list_debates(
    round_id: int,
    session: SessionDep,
    offset: int = 0,
    limit: int = Query(default=100, le=100),
) -> list[Debate]:
    get_or_404(session, Round, round_id)
    return session.exec(
        select(Debate).where(Debate.round_id == round_id).offset(offset).limit(limit)
    ).all()


@router.get("/debates/{debate_id}", response_model=DebatePublic)
def get_debate(debate_id: int, session: SessionDep) -> Debate:
    return get_or_404(session, Debate, debate_id)


@router.patch("/debates/{debate_id}", response_model=DebatePublic)
def update_debate(debate_id: int, debate_in: DebateUpdate, session: SessionDep) -> Debate:
    debate = get_or_404(session, Debate, debate_id)
    debate.sqlmodel_update(debate_in.model_dump(exclude_unset=True))
    session.add(debate)
    session.commit()
    session.refresh(debate)
    return debate


@router.delete("/debates/{debate_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_debate(debate_id: int, session: SessionDep) -> None:
    debate = get_or_404(session, Debate, debate_id)
    session.delete(debate)
    session.commit()
