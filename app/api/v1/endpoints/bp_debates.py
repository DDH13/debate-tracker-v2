from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.api.deps import SessionDep, get_or_404, require_format
from app.models import (
    BPBallot,
    BPDebate,
    BPDebateCreate,
    BPDebateJudge,
    BPDebateJudgeCreate,
    BPDebateJudgePublic,
    BPDebatePublic,
    BPDebateTeam,
    BPDebateTeamCreate,
    BPDebateTeamPublic,
    BPDebateUpdate,
    DebateFormat,
    Judge,
    Round,
    Team,
    Tournament,
)

router = APIRouter(tags=["bp-debates"])


class BPDebatePublicWithTeams(BPDebatePublic):
    teams: list[BPDebateTeamPublic]


def _validate_bp_teams(session: SessionDep, tournament_id: int, teams_in: list[BPDebateTeamCreate]) -> None:
    if len(teams_in) != 4:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A BP debate must have exactly 4 teams",
        )
    sides = {t.side for t in teams_in}
    if len(sides) != 4:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Each of OG/OO/CG/CO must appear exactly once",
        )
    team_ids = {t.team_id for t in teams_in}
    if len(team_ids) != 4:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="teams must reference 4 distinct teams",
        )
    for team_id in team_ids:
        team = get_or_404(session, Team, team_id)
        if team.tournament_id != tournament_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Team {team_id} does not belong to this tournament",
            )


@router.post(
    "/rounds/{round_id}/bp-debates",
    response_model=BPDebatePublicWithTeams,
    status_code=status.HTTP_201_CREATED,
)
def create_bp_debate(round_id: int, debate_in: BPDebateCreate, session: SessionDep) -> BPDebate:
    round_ = get_or_404(session, Round, round_id)
    tournament = get_or_404(session, Tournament, round_.tournament_id)
    require_format(tournament, DebateFormat.BP)
    _validate_bp_teams(session, round_.tournament_id, debate_in.teams)

    debate = BPDebate(round_id=round_id, room=debate_in.room)
    session.add(debate)
    session.flush()
    for team_in in debate_in.teams:
        session.add(BPDebateTeam(bp_debate_id=debate.id, team_id=team_in.team_id, side=team_in.side))
    session.commit()
    session.refresh(debate)
    return debate


@router.get("/rounds/{round_id}/bp-debates", response_model=list[BPDebatePublicWithTeams])
def list_bp_debates(
    round_id: int,
    session: SessionDep,
    offset: int = 0,
    limit: int = Query(default=100, le=100),
) -> list[BPDebate]:
    get_or_404(session, Round, round_id)
    return session.exec(
        select(BPDebate).where(BPDebate.round_id == round_id).offset(offset).limit(limit)
    ).all()


@router.get("/bp-debates/{debate_id}", response_model=BPDebatePublicWithTeams)
def get_bp_debate(debate_id: int, session: SessionDep) -> BPDebate:
    return get_or_404(session, BPDebate, debate_id)


@router.patch("/bp-debates/{debate_id}", response_model=BPDebatePublicWithTeams)
def update_bp_debate(debate_id: int, debate_in: BPDebateUpdate, session: SessionDep) -> BPDebate:
    debate = get_or_404(session, BPDebate, debate_id)
    debate.sqlmodel_update(debate_in.model_dump(exclude_unset=True))
    session.add(debate)
    session.commit()
    session.refresh(debate)
    return debate


@router.delete("/bp-debates/{debate_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bp_debate(debate_id: int, session: SessionDep) -> None:
    debate = get_or_404(session, BPDebate, debate_id)
    session.delete(debate)
    session.commit()


# --- Panel allocation ---


@router.post(
    "/bp-debates/{debate_id}/judges",
    response_model=BPDebateJudgePublic,
    status_code=status.HTTP_201_CREATED,
)
def add_bp_debate_judge(
    debate_id: int, judge_in: BPDebateJudgeCreate, session: SessionDep
) -> BPDebateJudge:
    get_or_404(session, BPDebate, debate_id)
    get_or_404(session, Judge, judge_in.judge_id)
    link = BPDebateJudge.model_validate(judge_in, update={"bp_debate_id": debate_id})
    session.add(link)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Judge is already on this panel",
        ) from exc
    session.refresh(link)
    return link


@router.get("/bp-debates/{debate_id}/judges", response_model=list[BPDebateJudgePublic])
def list_bp_debate_judges(debate_id: int, session: SessionDep) -> list[BPDebateJudge]:
    get_or_404(session, BPDebate, debate_id)
    return session.exec(select(BPDebateJudge).where(BPDebateJudge.bp_debate_id == debate_id)).all()


@router.delete(
    "/bp-debates/{debate_id}/judges/{judge_id}", status_code=status.HTTP_204_NO_CONTENT
)
def remove_bp_debate_judge(debate_id: int, judge_id: int, session: SessionDep) -> None:
    get_or_404(session, BPDebate, debate_id)
    link = session.exec(
        select(BPDebateJudge).where(
            BPDebateJudge.bp_debate_id == debate_id, BPDebateJudge.judge_id == judge_id
        )
    ).first()
    if link is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Judge {judge_id} is not on the panel for debate {debate_id}",
        )
    ballot = session.exec(
        select(BPBallot).where(BPBallot.bp_debate_id == debate_id, BPBallot.judge_id == judge_id)
    ).first()
    if ballot is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot remove judge from panel: they have submitted a ballot",
        )
    session.delete(link)
    session.commit()
