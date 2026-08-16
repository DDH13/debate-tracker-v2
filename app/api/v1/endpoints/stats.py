from fastapi import APIRouter, HTTPException, Query, status
from sqlmodel import select

from app.api.deps import SessionDep, get_or_404, require_format
from app.models import (
    DebateFormat,
    Debater,
    DebaterProfile,
    DebaterProfilePublic,
    Institution,
    Judge,
    JudgeProfile,
    JudgeProfilePublic,
    Tournament,
)
from app.services import stats

router = APIRouter(tags=["stats"])

_DEBATER_SORT_FIELDS = {
    "speaker_rank",
    "speaker_score_percentile",
    "activity_percentile",
    "win_rate_prelims_percentile",
    "win_rate_elims_percentile",
    "average_speaker_score",
    "total_rounds",
}
_JUDGE_SORT_FIELDS = {
    "activity_percentile",
    "dissent_rate",
    "overall_sentiment",
    "total_judged",
    "score_stdev",
}


# --- Tournament-scoped ---


@router.get("/tournaments/{tournament_id}/speaker-tab", response_model=stats.SpeakerTab)
def get_speaker_tab(tournament_id: int, session: SessionDep) -> stats.SpeakerTab:
    get_or_404(session, Tournament, tournament_id)
    return stats.speaker_tab(session, tournament_id)


@router.get("/tournaments/{tournament_id}/team-standings", response_model=list[stats.TeamStanding])
def get_team_standings(tournament_id: int, session: SessionDep) -> list[stats.TeamStanding]:
    get_or_404(session, Tournament, tournament_id)
    return stats.team_standings(session, tournament_id)


@router.get("/tournaments/{tournament_id}/summary", response_model=stats.TournamentSummary)
def get_tournament_summary(tournament_id: int, session: SessionDep) -> stats.TournamentSummary:
    get_or_404(session, Tournament, tournament_id)
    return stats.tournament_summary(session, tournament_id)


@router.get("/tournaments/{tournament_id}/side-stats", response_model=stats.SideStats)
def get_side_stats(tournament_id: int, session: SessionDep) -> stats.SideStats:
    get_or_404(session, Tournament, tournament_id)
    return stats.side_stats(session, tournament_id)


@router.get("/tournaments/{tournament_id}/motion-stats", response_model=list[stats.MotionStat])
def get_motion_stats(tournament_id: int, session: SessionDep) -> list[stats.MotionStat]:
    get_or_404(session, Tournament, tournament_id)
    return stats.motion_stats(session, tournament_id)


# --- BP tournament-scoped ---


@router.get("/tournaments/{tournament_id}/bp/speaker-tab", response_model=stats.BPSpeakerTab)
def get_bp_speaker_tab(tournament_id: int, session: SessionDep) -> stats.BPSpeakerTab:
    tournament = get_or_404(session, Tournament, tournament_id)
    require_format(tournament, DebateFormat.BP)
    return stats.bp_speaker_tab(session, tournament_id)


@router.get(
    "/tournaments/{tournament_id}/bp/team-standings", response_model=list[stats.BPTeamStanding]
)
def get_bp_team_standings(tournament_id: int, session: SessionDep) -> list[stats.BPTeamStanding]:
    tournament = get_or_404(session, Tournament, tournament_id)
    require_format(tournament, DebateFormat.BP)
    return stats.bp_team_standings(session, tournament_id)


@router.get("/tournaments/{tournament_id}/bp/summary", response_model=stats.BPTournamentSummary)
def get_bp_tournament_summary(tournament_id: int, session: SessionDep) -> stats.BPTournamentSummary:
    tournament = get_or_404(session, Tournament, tournament_id)
    require_format(tournament, DebateFormat.BP)
    return stats.bp_tournament_summary(session, tournament_id)


@router.get("/tournaments/{tournament_id}/bp/side-stats", response_model=stats.BPSideStats)
def get_bp_side_stats(tournament_id: int, session: SessionDep) -> stats.BPSideStats:
    tournament = get_or_404(session, Tournament, tournament_id)
    require_format(tournament, DebateFormat.BP)
    return stats.bp_side_stats(session, tournament_id)


@router.get(
    "/tournaments/{tournament_id}/bp/motion-stats", response_model=list[stats.BPMotionStat]
)
def get_bp_motion_stats(tournament_id: int, session: SessionDep) -> list[stats.BPMotionStat]:
    tournament = get_or_404(session, Tournament, tournament_id)
    require_format(tournament, DebateFormat.BP)
    return stats.bp_motion_stats(session, tournament_id)


# --- Materialized profiles ---


@router.post("/stats/refresh", response_model=stats.RefreshResult)
def refresh_stats(session: SessionDep) -> stats.RefreshResult:
    return stats.refresh_profiles(session)


@router.get("/debaters/{debater_id}/profile", response_model=DebaterProfilePublic)
def get_debater_profile(debater_id: int, session: SessionDep) -> DebaterProfile:
    get_or_404(session, Debater, debater_id)
    profile = session.exec(
        select(DebaterProfile).where(DebaterProfile.debater_id == debater_id)
    ).first()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No profile for debater {debater_id}; run POST /stats/refresh first",
        )
    return profile


@router.get("/judges/{judge_id}/profile", response_model=JudgeProfilePublic)
def get_judge_profile(judge_id: int, session: SessionDep) -> JudgeProfile:
    get_or_404(session, Judge, judge_id)
    profile = session.exec(select(JudgeProfile).where(JudgeProfile.judge_id == judge_id)).first()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No profile for judge {judge_id}; run POST /stats/refresh first",
        )
    return profile


@router.get("/stats/debater-profiles", response_model=list[DebaterProfilePublic])
def list_debater_profiles(
    session: SessionDep,
    sort_by: str = "speaker_rank",
    offset: int = 0,
    limit: int = Query(default=100, le=100),
) -> list[DebaterProfile]:
    if sort_by not in _DEBATER_SORT_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"sort_by must be one of {sorted(_DEBATER_SORT_FIELDS)}",
        )
    column = getattr(DebaterProfile, sort_by)
    ordering = column.asc() if sort_by == "speaker_rank" else column.desc()
    return session.exec(
        select(DebaterProfile).order_by(ordering).offset(offset).limit(limit)
    ).all()


@router.get("/stats/judge-profiles", response_model=list[JudgeProfilePublic])
def list_judge_profiles(
    session: SessionDep,
    sort_by: str = "activity_percentile",
    offset: int = 0,
    limit: int = Query(default=100, le=100),
) -> list[JudgeProfile]:
    if sort_by not in _JUDGE_SORT_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"sort_by must be one of {sorted(_JUDGE_SORT_FIELDS)}",
        )
    column = getattr(JudgeProfile, sort_by)
    return session.exec(
        select(JudgeProfile).order_by(column.desc()).offset(offset).limit(limit)
    ).all()


# --- Judge sentiment ---


@router.get("/stats/judge-sentiment", response_model=list[stats.JudgeSentiment])
def get_judge_sentiment(
    session: SessionDep, allowed_deviation: float = 0.5
) -> list[stats.JudgeSentiment]:
    return stats.judge_sentiment(session, allowed_deviation)


# --- Global / cross-tournament ---


@router.get("/stats/global-distribution", response_model=stats.GlobalDistribution)
def get_global_distribution(session: SessionDep) -> stats.GlobalDistribution:
    return stats.global_distribution(session)


@router.get("/stats/speaker-leaderboard", response_model=list[stats.LeaderboardEntry])
def get_speaker_leaderboard(
    session: SessionDep, limit: int = Query(default=10, le=100)
) -> list[stats.LeaderboardEntry]:
    return stats.speaker_leaderboard(session, limit)


@router.get("/debaters/{debater_id}/head-to-head", response_model=list[stats.HeadToHeadRecord])
def get_head_to_head(
    debater_id: int, session: SessionDep, opponent_id: int | None = None
) -> list[stats.HeadToHeadRecord]:
    get_or_404(session, Debater, debater_id)
    if opponent_id is not None:
        get_or_404(session, Debater, opponent_id)
    return stats.head_to_head(session, debater_id, opponent_id)


@router.get("/institutions/{institution_id}/stats", response_model=stats.InstitutionStats)
def get_institution_stats(institution_id: int, session: SessionDep) -> stats.InstitutionStats:
    get_or_404(session, Institution, institution_id)
    return stats.institution_stats(session, institution_id)
