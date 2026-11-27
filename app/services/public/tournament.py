"""`GET /public/tournaments/{id}`: everything a tournament page needs in one response."""

from sqlalchemy import Integer, cast
from sqlmodel import Session, SQLModel, func, select

from app.models import Debate, DebateJudge, Judge, Round
from app.services.public import scores
from app.services.public.loaders import institution_refs, judge_refs
from app.services.public.schemas import (
    DebateVotes,
    InstitutionRef,
    PersonRef,
    RoundRef,
    TeamRef,
    TournamentRef,
    WinLoss,
    win_loss,
)
from app.services.public.snapshots import TournamentSnapshot
from app.services.stats import tournament as tournament_stats


class BracketDebate(SQLModel):
    debate_id: int
    prop_team: TeamRef
    opp_team: TeamRef
    winner_team_id: int | None
    votes: DebateVotes
    margin: float | None


class BracketRound(SQLModel):
    round: RoundRef
    debates: list[BracketDebate]


class BracketOut(SQLModel):
    label: str
    primary: bool
    champion: TeamRef | None
    finalist: TeamRef | None
    rounds: list[BracketRound]


class StandingRow(SQLModel):
    rank: int
    team: TeamRef
    speakers: list[PersonRef]
    prelim: WinLoss
    elim: WinLoss
    total_speaks: float
    average_speaks: float
    finish: str | None
    bracket: str | None


class SpeakerRow(SQLModel):
    rank: int | None  # None: below the minimum number of speeches
    debater: PersonRef
    team: TeamRef | None
    institution: InstitutionRef | None
    average: float
    stdev: float
    speeches: int


class RoundOut(SQLModel):
    round: RoundRef
    motion: str | None
    info_slide: str | None
    debates: int
    prop_wins: int
    opp_wins: int
    prop_win_rate: float | None
    average_prop_speaks: float | None
    average_opp_speaks: float | None


class JudgeRow(SQLModel):
    judge: PersonRef
    institution: InstitutionRef | None
    debates: int
    chaired: int
    highest_round: RoundRef | None


class TournamentSummaryOut(SQLModel):
    institutions: int
    teams: int
    debaters: int
    judges: int
    rounds: int
    prelim_rounds: int
    elim_rounds: int
    debates: int
    mean_speaker_score: float | None
    prop_wins: int
    opp_wins: int
    prop_win_rate: float | None


class PublicTournament(SQLModel):
    tournament: TournamentRef
    summary: TournamentSummaryOut
    champion: TeamRef | None
    finalist: TeamRef | None
    brackets: list[BracketOut]
    standings: list[StandingRow]
    speakers: list[SpeakerRow]
    minimum_speeches: int
    rounds: list[RoundOut]
    judges: list[JudgeRow]


def team_ref_map(session: Session, snapshot: TournamentSnapshot) -> dict[int, TeamRef]:
    institutions = institution_refs(session, (t.institution_id for t in snapshot.teams.values()))
    return {
        t.id: TeamRef(id=t.id, name=t.name, institution=institutions.get(t.institution_id))
        for t in snapshot.teams.values()
    }


def build_public_tournament(session: Session, snapshot: TournamentSnapshot) -> PublicTournament:
    tid = snapshot.tournament.id
    teams = team_ref_map(session, snapshot)
    round_refs = {
        r.id: RoundRef(id=r.id, seq=r.seq, name=r.name, abbr=r.abbr, is_elimination=r.is_elimination)
        for r in snapshot.rounds
    }
    margins = scores.debate_margins(session, tournament_id=tid)
    votes = scores.debate_votes(session, tournament_id=tid)

    brackets = [
        BracketOut(
            label=bracket.label,
            primary=bracket.primary,
            champion=teams.get(bracket.champion_team_id),
            finalist=teams.get(bracket.finalist_team_id),
            rounds=[
                BracketRound(
                    round=round_refs[round_.round_id],
                    debates=[
                        BracketDebate(
                            debate_id=d.debate_id,
                            prop_team=teams[d.prop_team_id],
                            opp_team=teams[d.opp_team_id],
                            winner_team_id=d.winner_team_id,
                            votes=DebateVotes(
                                prop=votes.get(d.debate_id, (0, 0))[0], opp=votes.get(d.debate_id, (0, 0))[1]
                            ),
                            margin=margins.get(d.debate_id),
                        )
                        for d in round_.debates
                    ],
                )
                for round_ in bracket.rounds
            ],
        )
        for bracket in snapshot.brackets
    ]
    primary = next((b for b in brackets if b.primary), None)

    standings = []
    for entry in snapshot.standings:
        team = snapshot.teams.get(entry.team_id)
        if team is None or entry.team_id not in teams:
            continue
        finish = snapshot.finishes.get(entry.team_id)
        standings.append(
            StandingRow(
                rank=entry.rank,
                team=teams[entry.team_id],
                speakers=[
                    PersonRef(id=d, name=snapshot.debater_names.get(d, f"Debater {d}"))
                    for d in team.debater_ids
                ],
                prelim=win_loss(entry.prelim_wins, entry.prelim_losses),
                elim=win_loss(entry.elim_wins, entry.elim_losses),
                total_speaks=entry.total_speaks,
                average_speaks=entry.average_speaks,
                finish=finish.finish if finish else None,
                bracket=finish.bracket_label if finish else None,
            )
        )

    speakers = [
        SpeakerRow(
            rank=e.rank,
            debater=PersonRef(id=e.debater_id, name=e.name),
            team=teams.get(e.team_id),
            institution=teams[e.team_id].institution if e.team_id in teams else None,
            average=e.average,
            stdev=e.stdev,
            speeches=e.speeches,
        )
        for e in snapshot.tab
    ]

    rounds = [
        RoundOut(
            round=round_refs[r.id],
            motion=r.motion,
            info_slide=r.info_slide,
            debates=r.debates,
            prop_wins=r.prop_wins,
            opp_wins=r.opp_wins,
            prop_win_rate=round(r.prop_wins / (r.prop_wins + r.opp_wins) * 100, 2)
            if r.prop_wins + r.opp_wins
            else None,
            average_prop_speaks=r.average_prop_speaks,
            average_opp_speaks=r.average_opp_speaks,
        )
        for r in snapshot.rounds
    ]

    summary = tournament_stats.tournament_summary(session, tid)
    return PublicTournament(
        tournament=snapshot.tournament,
        summary=TournamentSummaryOut(
            institutions=summary.institutions,
            teams=summary.teams,
            debaters=summary.debaters,
            judges=summary.judges,
            rounds=summary.rounds,
            prelim_rounds=summary.prelim_rounds,
            elim_rounds=summary.elim_rounds,
            debates=summary.debates,
            mean_speaker_score=summary.mean_speaker_score,
            prop_wins=summary.prop_wins,
            opp_wins=summary.opp_wins,
            prop_win_rate=summary.prop_win_rate,
        ),
        champion=primary.champion if primary else None,
        finalist=primary.finalist if primary else None,
        brackets=brackets,
        standings=standings,
        speakers=speakers,
        minimum_speeches=snapshot.minimum_speeches,
        rounds=rounds,
        judges=_judge_rows(session, tid, round_refs),
    )


def _judge_rows(session: Session, tournament_id: int, round_refs: dict[int, RoundRef]) -> list[JudgeRow]:
    rows = session.exec(
        select(
            DebateJudge.judge_id,
            func.count(func.distinct(DebateJudge.debate_id)),
            func.sum(cast(DebateJudge.is_chair, Integer)),
            func.max(Round.seq),
        )
        .join(Debate, DebateJudge.debate_id == Debate.id)
        .join(Round, Debate.round_id == Round.id)
        .where(Round.tournament_id == tournament_id, DebateJudge.is_trainee == False)  # noqa: E712
        .group_by(DebateJudge.judge_id)
    ).all()
    if not rows:
        return []
    by_seq = {r.seq: r for r in round_refs.values()}
    judges = {
        j.id: j for j in session.exec(select(Judge).where(Judge.id.in_([r[0] for r in rows]))).all()
    }
    people = judge_refs(session, judges)
    institutions = institution_refs(session, (j.institution_id for j in judges.values()))
    result = [
        JudgeRow(
            judge=people[judge_id],
            institution=institutions.get(judges[judge_id].institution_id),
            debates=count,
            chaired=int(chaired or 0),
            highest_round=by_seq.get(top_seq),
        )
        for judge_id, count, chaired, top_seq in rows
        if judge_id in people
    ]
    result.sort(key=lambda r: (-(r.highest_round.seq if r.highest_round else 0), -r.debates, r.judge.name))
    return result
