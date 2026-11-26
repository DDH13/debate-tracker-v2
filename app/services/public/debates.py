"""The debate explorer (`GET /public/debates`) and a single debate's ballot sheet
(`GET /public/debates/{id}`)."""

from collections import defaultdict
from typing import Literal

from sqlalchemy.orm import aliased
from sqlmodel import Session, SQLModel, col, exists, func, or_, select

from app.models import (
    Ballot,
    Debate,
    DebateJudge,
    Judge,
    Motion,
    Round,
    Side,
    SpeakerScore,
    Team,
    TeamMember,
    Tournament,
)
from app.services.public import scores
from app.services.public.loaders import (
    debater_refs,
    judge_refs,
    round_ref,
    team_refs,
    tournament_ref,
)
from app.services.public.schemas import (
    DebateRow,
    DebateVotes,
    MotionExcerpt,
    Page,
    PersonRef,
    RoundRef,
    TeamRef,
    TournamentRef,
)

Stage = Literal["prelim", "elim"]


class DebateList(Page):
    items: list[DebateRow]


def list_debates(
    session: Session,
    *,
    tournament_id: int | None,
    round_id: int | None,
    team_id: int | None,
    debater_id: int | None,
    judge_id: int | None,
    institution_id: int | None,
    stage: Stage | None,
    q: str | None,
    offset: int,
    limit: int,
) -> DebateList:
    prop_team, opp_team = aliased(Team), aliased(Team)
    base = (
        select(Debate.id)
        .join(Round, Debate.round_id == Round.id)
        .join(Tournament, Round.tournament_id == Tournament.id)
        .outerjoin(Motion, Motion.round_id == Round.id)
    )
    if institution_id is not None:
        base = base.join(prop_team, Debate.prop_team_id == prop_team.id).join(
            opp_team, Debate.opp_team_id == opp_team.id
        )
        base = base.where(or_(prop_team.institution_id == institution_id, opp_team.institution_id == institution_id))
    if tournament_id is not None:
        base = base.where(Round.tournament_id == tournament_id)
    if round_id is not None:
        base = base.where(Debate.round_id == round_id)
    if stage is not None:
        base = base.where(Round.isElimination == (stage == "elim"))
    if team_id is not None:
        base = base.where(or_(Debate.prop_team_id == team_id, Debate.opp_team_id == team_id))
    if debater_id is not None:
        # Roster-based: any debate played by a team this debater was on.
        teams = select(TeamMember.team_id).where(TeamMember.debater_id == debater_id)
        base = base.where(or_(col(Debate.prop_team_id).in_(teams), col(Debate.opp_team_id).in_(teams)))
    if judge_id is not None:
        base = base.where(
            exists().where(DebateJudge.debate_id == Debate.id, DebateJudge.judge_id == judge_id)
        )
    if q and q.strip():
        base = base.where(col(Motion.text).icontains(q.strip(), autoescape=True))

    total = session.exec(select(func.count()).select_from(base.subquery())).one()
    page_rows = session.execute(
        base.add_columns(Round.seq, Tournament.date, Tournament.id)
        .order_by(
            col(Tournament.date).desc().nulls_last(), col(Tournament.id).desc(), Round.seq, Debate.id
        )
        .offset(offset)
        .limit(limit)
    ).all()
    ids = [r[0] for r in page_rows]
    return DebateList(total=total, offset=offset, limit=limit, items=_rows(session, ids))


def _rows(session: Session, ids: list[int]) -> list[DebateRow]:
    if not ids:
        return []
    found = session.exec(
        select(Debate, Round, Tournament, Motion.text)
        .join(Round, Debate.round_id == Round.id)
        .join(Tournament, Round.tournament_id == Tournament.id)
        .outerjoin(Motion, Motion.round_id == Round.id)
        .where(Debate.id.in_(ids))
    ).all()
    by_id = {d.id: (d, r, t, motion) for d, r, t, motion in found}
    teams = team_refs(session, [d.prop_team_id for d, *_ in found] + [d.opp_team_id for d, *_ in found])
    margins = scores.debate_margins(session, debate_ids=ids)
    votes = scores.debate_votes(session, debate_ids=ids)
    chairs = {
        debate_id: judge_id
        for debate_id, judge_id in session.exec(
            select(DebateJudge.debate_id, DebateJudge.judge_id).where(
                DebateJudge.debate_id.in_(ids), DebateJudge.is_chair == True  # noqa: E712
            )
        ).all()
    }
    judges = judge_refs(session, chairs.values())
    rows = []
    for debate_id in ids:
        debate, round_, tournament, motion = by_id[debate_id]
        prop_votes, opp_votes = votes.get(debate_id, (0, 0))
        rows.append(
            DebateRow(
                id=debate_id,
                tournament=tournament_ref(tournament),
                round=round_ref(round_),
                prop_team=teams[debate.prop_team_id],
                opp_team=teams[debate.opp_team_id],
                winner=debate.winner,
                votes=DebateVotes(prop=prop_votes, opp=opp_votes),
                margin=margins.get(debate_id),
                motion=motion,
                chair=judges.get(chairs.get(debate_id)),
            )
        )
    return rows


def debate_rows(session: Session, ids: list[int]) -> list[DebateRow]:
    """Rows for specific debates, in the order given."""
    return _rows(session, ids)


# --- Detail ---


class JudgeScore(SQLModel):
    judge_id: int
    content: float | None
    style: float | None
    strategy: float | None
    final_score: float
    discarded: bool


class SpeakerSheet(SQLModel):
    side: Side
    position: int
    debater: PersonRef
    average: float | None  # over non-discarded ballots
    content: float | None
    style: float | None
    strategy: float | None
    by_judge: list[JudgeScore]


class PanelMember(SQLModel):
    judge: PersonRef
    is_chair: bool
    is_trainee: bool
    submitted: bool
    winner: Side | None
    discarded: bool
    forfeit: bool


class PublicDebate(SQLModel):
    id: int
    tournament: TournamentRef
    round: RoundRef
    motion: MotionExcerpt | None
    room: str | None
    prop_team: TeamRef
    opp_team: TeamRef
    winner: Side | None
    votes: DebateVotes
    margin: float | None
    prop_total: float | None
    opp_total: float | None
    panel: list[PanelMember]
    speakers: list[SpeakerSheet]
    previous_debate_id: int | None
    next_debate_id: int | None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def build_public_debate(session: Session, debate: Debate) -> PublicDebate:
    round_ = session.get(Round, debate.round_id)
    tournament = session.get(Tournament, round_.tournament_id)
    motion = session.exec(select(Motion).where(Motion.round_id == round_.id)).first()
    teams = team_refs(session, [debate.prop_team_id, debate.opp_team_id])

    links = session.exec(select(DebateJudge).where(DebateJudge.debate_id == debate.id)).all()
    ballots = session.exec(select(Ballot).where(Ballot.debate_id == debate.id)).all()
    ballot_by_judge = {b.judge_id: b for b in ballots}
    sheet_rows = session.exec(
        select(SpeakerScore, Ballot.judge_id, Ballot.discarded, Ballot.forfeit)
        .join(Ballot, SpeakerScore.ballot_id == Ballot.id)
        .where(Ballot.debate_id == debate.id)
    ).all()
    judge_ids = {l.judge_id for l in links} | set(ballot_by_judge)
    judges = judge_refs(session, judge_ids)
    debaters = debater_refs(session, (s.debater_id for s, *_ in sheet_rows))

    panel = []
    for link in sorted(links, key=lambda l: (not l.is_chair, l.is_trainee, l.id)):
        ballot = ballot_by_judge.get(link.judge_id)
        panel.append(
            PanelMember(
                judge=judges[link.judge_id],
                is_chair=link.is_chair,
                is_trainee=link.is_trainee,
                submitted=ballot is not None,
                winner=ballot.winner if ballot else None,
                discarded=ballot.discarded if ballot else False,
                forfeit=ballot.forfeit if ballot else False,
            )
        )
    linked = {l.judge_id for l in links}
    for ballot in ballots:  # a ballot from a judge not on the panel list still counts
        if ballot.judge_id not in linked:
            panel.append(
                PanelMember(
                    judge=judges[ballot.judge_id],
                    is_chair=False,
                    is_trainee=False,
                    submitted=True,
                    winner=ballot.winner,
                    discarded=ballot.discarded,
                    forfeit=ballot.forfeit,
                )
            )

    grouped: dict[tuple[Side, int, int], list[tuple[SpeakerScore, int, bool]]] = defaultdict(list)
    totals: dict[tuple[int, Side], float] = defaultdict(float)
    for score, judge_id, discarded, forfeit in sheet_rows:
        grouped[(score.side, int(score.position), score.debater_id)].append((score, judge_id, discarded))
        if not discarded and not forfeit:
            totals[(score.ballot_id, score.side)] += score.final_score

    speakers = []
    for (side, position, debater_id), entries in sorted(
        grouped.items(), key=lambda kv: (kv[0][0].value != "prop", kv[0][1], kv[0][2])
    ):
        counted = [s for s, _j, discarded in entries if not discarded]
        speakers.append(
            SpeakerSheet(
                side=side,
                position=position,
                debater=debaters[debater_id],
                average=_mean([s.final_score for s in counted]),
                content=_mean([s.content for s in counted if s.content is not None]),
                style=_mean([s.style for s in counted if s.style is not None]),
                strategy=_mean([s.strategy for s in counted if s.strategy is not None]),
                by_judge=[
                    JudgeScore(
                        judge_id=judge_id,
                        content=s.content,
                        style=s.style,
                        strategy=s.strategy,
                        final_score=s.final_score,
                        discarded=discarded,
                    )
                    for s, judge_id, discarded in sorted(entries, key=lambda e: e[1])
                ],
            )
        )

    ballot_ids = {s.ballot_id for s, *_ in sheet_rows}
    prop_totals = [totals[(b, Side.PROP)] for b in ballot_ids if (b, Side.PROP) in totals]
    opp_totals = [totals[(b, Side.OPP)] for b in ballot_ids if (b, Side.OPP) in totals]
    prop_total, opp_total = _mean(prop_totals), _mean(opp_totals)
    prop_votes = sum(1 for b in ballots if not b.discarded and b.winner == Side.PROP)
    opp_votes = sum(1 for b in ballots if not b.discarded and b.winner == Side.OPP)

    order = (Round.seq, Debate.id)
    previous_id = session.exec(
        select(Debate.id)
        .join(Round, Debate.round_id == Round.id)
        .where(
            Round.tournament_id == tournament.id,
            or_(Round.seq < round_.seq, (Round.seq == round_.seq) & (Debate.id < debate.id)),
        )
        .order_by(col(Round.seq).desc(), col(Debate.id).desc())
        .limit(1)
    ).first()
    next_id = session.exec(
        select(Debate.id)
        .join(Round, Debate.round_id == Round.id)
        .where(
            Round.tournament_id == tournament.id,
            or_(Round.seq > round_.seq, (Round.seq == round_.seq) & (Debate.id > debate.id)),
        )
        .order_by(*order)
        .limit(1)
    ).first()

    return PublicDebate(
        id=debate.id,
        tournament=tournament_ref(tournament),
        round=round_ref(round_),
        motion=MotionExcerpt(text=motion.text, info_slide=motion.info_slide) if motion else None,
        room=debate.room,
        prop_team=teams[debate.prop_team_id],
        opp_team=teams[debate.opp_team_id],
        winner=debate.winner,
        votes=DebateVotes(prop=prop_votes, opp=opp_votes),
        margin=round(prop_total - opp_total, 4) if prop_total is not None and opp_total is not None else None,
        prop_total=prop_total,
        opp_total=opp_total,
        panel=panel,
        speakers=speakers,
        previous_debate_id=previous_id,
        next_debate_id=next_id,
    )
