from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.api.deps import SessionDep, get_or_404
from app.models import (
    Debater,
    Team,
    TeamCreate,
    TeamMember,
    TeamMemberCreate,
    TeamMemberPublic,
    TeamPublic,
    TeamUpdate,
    Tournament,
)

router = APIRouter(tags=["teams"])


@router.post(
    "/tournaments/{tournament_id}/teams",
    response_model=TeamPublic,
    status_code=status.HTTP_201_CREATED,
)
def create_team(tournament_id: int, team_in: TeamCreate, session: SessionDep) -> Team:
    get_or_404(session, Tournament, tournament_id)
    team = Team.model_validate(team_in, update={"tournament_id": tournament_id})
    session.add(team)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Team with this name already exists in this tournament",
        ) from exc
    session.refresh(team)
    return team


@router.get("/tournaments/{tournament_id}/teams", response_model=list[TeamPublic])
def list_teams(
    tournament_id: int,
    session: SessionDep,
    offset: int = 0,
    limit: int = Query(default=100, le=100),
) -> list[Team]:
    get_or_404(session, Tournament, tournament_id)
    return session.exec(
        select(Team).where(Team.tournament_id == tournament_id).offset(offset).limit(limit)
    ).all()


@router.get("/teams/{team_id}", response_model=TeamPublic)
def get_team(team_id: int, session: SessionDep) -> Team:
    return get_or_404(session, Team, team_id)


@router.patch("/teams/{team_id}", response_model=TeamPublic)
def update_team(team_id: int, team_in: TeamUpdate, session: SessionDep) -> Team:
    team = get_or_404(session, Team, team_id)
    team.sqlmodel_update(team_in.model_dump(exclude_unset=True))
    session.add(team)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Team with this name already exists in this tournament",
        ) from exc
    session.refresh(team)
    return team


@router.delete("/teams/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_team(team_id: int, session: SessionDep) -> None:
    team = get_or_404(session, Team, team_id)
    session.delete(team)
    session.commit()


@router.post(
    "/teams/{team_id}/members",
    response_model=TeamMemberPublic,
    status_code=status.HTTP_201_CREATED,
)
def add_team_member(
    team_id: int, member_in: TeamMemberCreate, session: SessionDep
) -> TeamMember:
    get_or_404(session, Team, team_id)
    get_or_404(session, Debater, member_in.debater_id)
    member = TeamMember.model_validate(member_in, update={"team_id": team_id})
    session.add(member)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Debater already on this team",
        ) from exc
    session.refresh(member)
    return member


@router.get("/teams/{team_id}/members", response_model=list[TeamMemberPublic])
def list_team_members(
    team_id: int,
    session: SessionDep,
    offset: int = 0,
    limit: int = Query(default=100, le=100),
) -> list[TeamMember]:
    get_or_404(session, Team, team_id)
    return session.exec(
        select(TeamMember).where(TeamMember.team_id == team_id).offset(offset).limit(limit)
    ).all()


@router.delete("/teams/{team_id}/members/{debater_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_team_member(team_id: int, debater_id: int, session: SessionDep) -> None:
    get_or_404(session, Team, team_id)
    member = session.exec(
        select(TeamMember).where(
            TeamMember.team_id == team_id, TeamMember.debater_id == debater_id
        )
    ).first()
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Debater {debater_id} is not a member of team {team_id}",
        )
    session.delete(member)
    session.commit()
