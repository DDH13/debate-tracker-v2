from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from sqlmodel import select

from app.api.deps import SessionDep, get_or_404
from app.models import (
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
from app.services.stats.core import display_name

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


# --- Raw score and history queries ---


@router.get("/debaters/{debater_id}/scores", response_model=stats.DebaterScores)
def get_debater_scores(
    debater_id: int,
    session: SessionDep,
    include_replies: bool = False,
    normalized: bool = False,
) -> stats.DebaterScores:
    """Counted speaker scores grouped per tournament. Raw mode lists every judge's score;
    `normalized=true` returns the tab's round scores (panel mean, replies dropped, iron max)."""
    debater = get_or_404(session, Debater, debater_id)
    results = stats.debater_scores(
        session, debater_id, normalized=normalized, include_replies=include_replies
    )
    if results:
        return results[0]
    return stats.DebaterScores(
        debater_id=debater_id,
        name=display_name(debater.full_name, debater.first_name, debater.last_name),
        normalized=normalized,
        tournaments=[],
    )


@router.get("/stats/debater-scores", response_model=list[stats.DebaterScores])
def list_debater_scores(
    session: SessionDep, normalized: bool = False, include_replies: bool = False
) -> list[stats.DebaterScores]:
    return stats.debater_scores(session, normalized=normalized, include_replies=include_replies)


@router.get("/debaters/{debater_id}/debates", response_model=list[stats.TournamentDebates])
def get_debater_debates(
    debater_id: int, session: SessionDep, stage: Literal["prelim", "elim"] | None = None
) -> list[stats.TournamentDebates]:
    get_or_404(session, Debater, debater_id)
    return stats.debater_debates(session, debater_id, stage)


@router.get("/debaters/{debater_id}/teams", response_model=list[stats.DebaterTeam])
def get_debater_teams(debater_id: int, session: SessionDep) -> list[stats.DebaterTeam]:
    get_or_404(session, Debater, debater_id)
    return stats.debater_teams(session, debater_id)


@router.get("/judges/{judge_id}/scores", response_model=list[stats.TournamentJudgeScores])
def get_judge_scores(
    judge_id: int, session: SessionDep, include_replies: bool = False
) -> list[stats.TournamentJudgeScores]:
    get_or_404(session, Judge, judge_id)
    return stats.judge_scores(session, judge_id, include_replies)


@router.get("/judges/{judge_id}/debates", response_model=list[stats.TournamentJudgeDebates])
def get_judge_debates(
    judge_id: int, session: SessionDep, stage: Literal["prelim", "elim"] | None = None
) -> list[stats.TournamentJudgeDebates]:
    get_or_404(session, Judge, judge_id)
    return stats.judge_debates(session, judge_id, stage)


@router.get("/stats/tournament-averages", response_model=list[stats.TournamentAverage])
def get_tournament_averages(session: SessionDep) -> list[stats.TournamentAverage]:
    return stats.tournament_averages(session)
