"""`GET /public/judges/{id}`: a judge's whole page in one response.

Their own history (debates, ballots, scores, dissents) is computed live; leniency, harshness
and sentiment come from the materialized `JudgeProfile`, and `profile_computed_at` is null
until `POST /stats/refresh` has run. Everything is public: the judge's tendencies are named."""

from collections import defaultdict
from datetime import date, datetime
from statistics import pstdev

from sqlmodel import Session, SQLModel, select

from app.models import (
    Ballot,
    Debate,
    DebateJudge,
    Judge,
    JudgeProfile,
    Round,
    Side,
    SpeakerPosition,
    SpeakerScore,
)
from app.services.public_site import debates as debate_service
from app.services.public_site import categories, field
from app.services.public_site.categories import JudgeCategories
from app.services.public_site.loaders import (
    institution_refs,
    judge_refs,
    person_name,
    round_ref,
    tournament_refs,
)
from app.services.public_site.schemas import (
    DebateRow,
    InstitutionRef,
    Metric,
    PersonRef,
    RoundRef,
    TournamentRef,
    metric,
)


class JudgeSummary(SQLModel):
    tournaments: int
    debates: int
    chaired: int
    panellist: int
    trainee: int
    ballots_cast: int
    average: Metric
    stdev: Metric
    dissent_rate: Metric
    sentiment: Metric
    leniency: Metric
    harshness: Metric


class JudgePositionCell(SQLModel):
    position: int  # 1-3 substantive; 4 reply (own scale)
    average: float | None
    count: int
    field_mean: float | None
    delta: float | None


class DecisionSplit(SQLModel):
    prop_votes: int
    opp_votes: int
    prop_rate: float | None
    field_prop_rate: float | None  # parity reference: share of all ballots cast for Proposition
    with_majority: int
    minority: int
    unanimous: int  # debates where every counted ballot agreed


class JudgeTournamentRow(SQLModel):
    tournament: TournamentRef
    debates: int
    chaired: int
    ballots: int
    dissents: int
    average: float | None
    highest_round: RoundRef | None


class PanelVote(SQLModel):
    judge: PersonRef
    is_chair: bool
    winner: Side | None
    discarded: bool


class JudgedDebate(SQLModel):
    debate: DebateRow
    is_chair: bool
    is_trainee: bool
    vote: Side | None  # this judge's ballot
    dissented: bool | None  # None when no ballot or no recorded winner
    panel: list[PanelVote]


class PublicJudge(SQLModel):
    judge: PersonRef
    institution: InstitutionRef | None
    profile_computed_at: datetime | None
    summary: JudgeSummary
    categories: JudgeCategories | None  # None below 5 counted ballots
    positions: list[JudgePositionCell]
    decisions: DecisionSplit
    tournaments: list[JudgeTournamentRow]
    debates: list[JudgedDebate]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def build_public_judge(session: Session, judge: Judge) -> PublicJudge:
    jid = judge.id
    means = field.field_means(session)
    profile = session.exec(select(JudgeProfile).where(JudgeProfile.judge_id == jid)).first()

    links = session.exec(
        select(DebateJudge, Debate, Round)
        .join(Debate, DebateJudge.debate_id == Debate.id)
        .join(Round, Debate.round_id == Round.id)
        .where(DebateJudge.judge_id == jid)
    ).all()
    my_ballots = session.exec(
        select(Ballot, Debate.winner, Debate.round_id)
        .join(Debate, Ballot.debate_id == Debate.id)
        .where(Ballot.judge_id == jid)
    ).all()
    debate_round: dict[int, Round] = {d.id: r for _l, d, r in links}
    # A ballot without a panel link still counts: fetch those rounds in one query.
    missing_rounds = {round_id for b, _w, round_id in my_ballots if b.debate_id not in debate_round}
    if missing_rounds:
        rounds_by_id = {r.id: r for r in session.exec(select(Round).where(Round.id.in_(missing_rounds))).all()}
        for ballot, _winner, round_id in my_ballots:
            if ballot.debate_id not in debate_round:
                debate_round[ballot.debate_id] = rounds_by_id[round_id]
    debate_ids = sorted(debate_round)

    # Position averages and per-tournament average: counted speeches this judge scored.
    position_scores: dict[int, list[float]] = defaultdict(list)
    tournament_scores: dict[int, list[float]] = defaultdict(list)
    for position, final_score, debate_id in session.exec(
        select(SpeakerScore.position, SpeakerScore.final_score, Ballot.debate_id)
        .join(Ballot, SpeakerScore.ballot_id == Ballot.id)
        .where(Ballot.judge_id == jid, Ballot.discarded == False, Ballot.forfeit == False)  # noqa: E712
    ).all():
        position_scores[int(position)].append(final_score)
        if position != SpeakerPosition.REPLY:
            tournament_scores[debate_round[debate_id].tournament_id].append(final_score)
    substantive = [v for p in (1, 2, 3) for v in position_scores.get(p, [])]

    # Everyone's ballots on this judge's debates: for panels, majority and unanimity.
    panel_ballots: dict[int, list[Ballot]] = defaultdict(list)
    panel_links: dict[int, list[DebateJudge]] = defaultdict(list)
    if debate_ids:
        for ballot in session.exec(select(Ballot).where(Ballot.debate_id.in_(debate_ids))).all():
            panel_ballots[ballot.debate_id].append(ballot)
        for link in session.exec(select(DebateJudge).where(DebateJudge.debate_id.in_(debate_ids))).all():
            panel_links[link.debate_id].append(link)
    people = judge_refs(
        session,
        {b.judge_id for bs in panel_ballots.values() for b in bs}
        | {l.judge_id for ls in panel_links.values() for l in ls},
    )

    # Decisions.
    winner_of = {b.debate_id: w for b, w, _r in my_ballots}
    counted = [(b, w) for b, w, _r in my_ballots if not b.discarded]
    prop_votes = sum(1 for b, _w in counted if b.winner == Side.PROP)
    opp_votes = len(counted) - prop_votes
    judged = [(b, w) for b, w in counted if w is not None]
    minority = sum(1 for b, w in judged if b.winner != w)
    unanimous = 0
    for debate_id in debate_ids:
        counted_ballots = [b for b in panel_ballots.get(debate_id, []) if not b.discarded]
        # A solo-judged debate is not a unanimous panel: it needs at least two counted ballots.
        if (
            len(counted_ballots) >= 2
            and len({b.winner for b in counted_ballots}) == 1
            and any(b.judge_id == jid for b in counted_ballots)
        ):
            unanimous += 1

    # Roles.
    chaired = sum(1 for l, _d, _r in links if l.is_chair and not l.is_trainee)
    trainee = sum(1 for l, _d, _r in links if l.is_trainee)
    panellist = len(links) - chaired - trainee

    # Rows.
    debate_rows = {row.id: row for row in debate_service.debate_rows(session, debate_ids)}
    link_by_debate = {l.debate_id: l for l, _d, _r in links}
    my_ballot_by_debate = {b.debate_id: b for b, _w, _r in my_ballots}
    judged_debates = []
    ordered = sorted(
        debate_ids,
        key=lambda d: (
            debate_rows[d].tournament.date or date.min,
            debate_rows[d].tournament.id,
            debate_rows[d].round.seq,
            d,
        ),
        reverse=True,
    )
    for debate_id in ordered:
        link = link_by_debate.get(debate_id)
        mine = my_ballot_by_debate.get(debate_id)
        winner = winner_of.get(debate_id)
        votes_by_judge = {b.judge_id: b for b in panel_ballots.get(debate_id, [])}
        members = sorted(
            {l.judge_id: l for l in panel_links.get(debate_id, [])}.values(),
            key=lambda l: (not l.is_chair, l.is_trainee, l.id),
        )
        judged_debates.append(
            JudgedDebate(
                debate=debate_rows[debate_id],
                is_chair=bool(link and link.is_chair),
                is_trainee=bool(link and link.is_trainee),
                vote=mine.winner if mine and not mine.discarded else None,
                dissented=None
                if mine is None or mine.discarded or winner is None
                else mine.winner != winner,
                panel=[
                    PanelVote(
                        judge=people[l.judge_id],
                        is_chair=l.is_chair,
                        winner=votes_by_judge[l.judge_id].winner if l.judge_id in votes_by_judge else None,
                        discarded=votes_by_judge[l.judge_id].discarded if l.judge_id in votes_by_judge else False,
                    )
                    for l in members
                ],
            )
        )

    # Per tournament.
    by_tournament: dict[int, dict] = defaultdict(
        lambda: {"debates": set(), "chaired": 0, "ballots": 0, "dissents": 0, "top": None}
    )
    for debate_id, round_ in debate_round.items():
        entry = by_tournament[round_.tournament_id]
        entry["debates"].add(debate_id)
        if entry["top"] is None or round_.seq > entry["top"].seq:
            entry["top"] = round_
    for l, _d, r in links:
        if l.is_chair and not l.is_trainee:
            by_tournament[r.tournament_id]["chaired"] += 1
    for b, w in counted:
        entry = by_tournament[debate_round[b.debate_id].tournament_id]
        entry["ballots"] += 1
        if w is not None and b.winner != w:
            entry["dissents"] += 1
    refs = tournament_refs(session, by_tournament)
    tournaments = sorted(
        (
            JudgeTournamentRow(
                tournament=refs[tid],
                debates=len(entry["debates"]),
                chaired=entry["chaired"],
                ballots=entry["ballots"],
                dissents=entry["dissents"],
                average=None if not tournament_scores.get(tid) else round(_mean(tournament_scores[tid]), 4),
                highest_round=round_ref(entry["top"]) if entry["top"] else None,
            )
            for tid, entry in by_tournament.items()
        ),
        key=lambda r: (r.tournament.date or date.min, r.tournament.id),
        reverse=True,
    )

    field_positions = {1: means.judge_first, 2: means.judge_second, 3: means.judge_third, 4: means.judge_reply}
    positions = []
    for position in (1, 2, 3, 4):
        values = position_scores.get(position, [])
        avg = _mean(values)
        reference = field_positions[position]
        positions.append(
            JudgePositionCell(
                position=position,
                average=None if avg is None else round(avg, 4),
                count=len(values),
                field_mean=None if reference is None else round(reference, 4),
                delta=None if avg is None or reference is None else round(avg - reference, 2),
            )
        )

    dissent_rate = minority / len(judged) * 100 if judged else None
    institution = (
        institution_refs(session, [judge.institution_id]).get(judge.institution_id)
        if judge.institution_id is not None
        else None
    )
    cast = prop_votes + opp_votes
    return PublicJudge(
        judge=PersonRef(id=jid, name=person_name(judge, "Judge")),
        institution=institution,
        profile_computed_at=profile.computed_at if profile else means.profile_computed_at,
        summary=JudgeSummary(
            tournaments=len(by_tournament),
            debates=len(debate_ids),
            chaired=chaired,
            panellist=panellist,
            trainee=trainee,
            ballots_cast=cast,
            average=metric(_mean(substantive), means.judge_average),
            stdev=metric(pstdev(substantive) if substantive else None, means.judge_stdev),
            dissent_rate=metric(dissent_rate, means.judge_dissent_rate, digits=1),
            sentiment=metric(profile.overall_sentiment if profile else None, means.judge_sentiment),
            leniency=metric(profile.leniency if profile else None, means.judge_leniency),
            harshness=metric(profile.harshness if profile else None, means.judge_harshness),
        ),
        categories=categories.judge_categories(categories.category_data(session), jid),
        positions=positions,
        decisions=DecisionSplit(
            prop_votes=prop_votes,
            opp_votes=opp_votes,
            prop_rate=round(prop_votes / cast * 100, 2) if cast else None,
            field_prop_rate=None if means.ballot_prop_rate is None else round(means.ballot_prop_rate, 2),
            with_majority=len(judged) - minority,
            minority=minority,
            unanimous=unanimous,
        ),
        tournaments=tournaments,
        debates=judged_debates,
    )
