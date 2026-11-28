"""Split part of a debater's, judge's or school's history off by tournament.

The counterpart of `merge.py`: a merge (or an import that matched the wrong record) can join
two identities, and this moves the rows of the chosen tournaments to a new record or an
existing one. Like a merge it validates first, mutates, then commits once; `dry_run` does
everything and rolls back, so the console can preview. Errors are the merge service's
(`MergeNotFound` / `MergeInvalid` / `MergeConflict`, mapped by `merge_errors()`).

Only tournament-scoped rows move. Personal fields, squad places, judge records and
affiliations, memberships, contacts and workshops stay with the original record.
"""

from collections import defaultdict
from datetime import date

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, col, func, select, update

from app.models import (
    Ballot,
    Debate,
    DebateJudge,
    Debater,
    DebaterCreate,
    DebaterProfile,
    Feedback,
    Institution,
    InstitutionCreate,
    Judge,
    JudgeCreate,
    JudgeProfile,
    Round,
    SchoolAchievement,
    SpeakerScore,
    Team,
    TeamMember,
    Tournament,
    TournamentRegistration,
    Transaction,
)
from app.services.merge import MergeConflict, MergeInvalid, MergeNotFound
from app.services.text import split_name


class SplitRequest(SQLModel):
    tournament_ids: list[int]
    # An existing record to move the rows to; omitted, a new record is created.
    target_id: int | None = None
    dry_run: bool = False


class DebaterSplitRequest(SplitRequest):
    # Fields for the new record. Unset name fields are copied from the original; the school
    # defaults to the team's school at the most recent moved tournament. Personal fields are
    # never copied: nobody can tell whose they are.
    new: DebaterCreate | None = None


class JudgeSplitRequest(SplitRequest):
    new: JudgeCreate | None = None


class InstitutionSplitRequest(SplitRequest):
    # Required when no target is given: school names are unique.
    new: InstitutionCreate | None = None


class SplitTournament(SQLModel):
    tournament_id: int
    name: str
    abbr: str | None
    date: date | None
    counts: dict[str, int]


class SplitResult(SQLModel):
    # None on a dry run that would create the record.
    target_id: int | None
    target_name: str
    created: bool
    dry_run: bool
    moved: dict[str, int]
    # People whose own school moves with an institution split.
    school_moves: list[str] = []


def _load(session: Session, model: type, id_: int):
    obj = session.get(model, id_)
    if obj is None:
        raise MergeNotFound(f"{model.__name__} {id_} not found")
    return obj


def _label(person) -> str:
    return person.full_name or " ".join(p for p in (person.first_name, person.last_name) if p) or f"#{person.id}"


def _tournament_rows(session: Session, counts: dict[int, dict[str, int]], kinds: tuple[str, ...]) -> list[SplitTournament]:
    if not counts:
        return []
    tournaments = session.exec(select(Tournament).where(col(Tournament.id).in_(list(counts)))).all()
    rows = [
        SplitTournament(
            tournament_id=t.id,
            name=t.name,
            abbr=t.abbr,
            date=t.date,
            counts={kind: counts[t.id].get(kind, 0) for kind in kinds},
        )
        for t in tournaments
    ]
    rows.sort(key=lambda r: (r.date is None, -(r.date.toordinal() if r.date else 0), -r.tournament_id))
    return rows


def _grouped(session: Session, statement) -> dict[int, int]:
    return {tid: n for tid, n in session.exec(statement).all()}


def _merge_counts(**groups: dict[int, int]) -> dict[int, dict[str, int]]:
    counts: dict[int, dict[str, int]] = defaultdict(dict)
    for kind, group in groups.items():
        for tid, n in group.items():
            counts[tid][kind] = n
    return counts


def _validate(source_id: int, request: SplitRequest, involved: set[int], kind: str) -> set[int]:
    chosen = set(request.tournament_ids)
    if not chosen:
        raise MergeInvalid("Choose at least one tournament to split off")
    stray = sorted(chosen - involved)
    if stray:
        raise MergeInvalid(f"This {kind} has nothing at tournament(s) {', '.join(map(str, stray))}")
    if request.target_id == source_id:
        raise MergeInvalid("The target must be a different record")
    if request.target_id is None and chosen == involved:
        raise MergeInvalid(
            f"That is every tournament this {kind} has: nothing would be left, so edit the record instead"
        )
    return chosen


def _person_fields(source, new: SQLModel | None, **defaults) -> dict:
    """The new person's fields: the source's name and `defaults`, then whatever `new` sets. A
    new full name without first / last names gets them from `split_name`, not the source's."""
    fields = {"full_name": source.full_name, "first_name": source.first_name, "last_name": source.last_name, **defaults}
    given = new.model_dump(exclude_unset=True) if new is not None else {}
    if given.get("full_name") and not ({"first_name", "last_name"} & set(given)):
        fields["first_name"], fields["last_name"] = split_name(given["full_name"])
    return {**fields, **given}


def _finish(session: Session, result: SplitResult, dry_run: bool) -> SplitResult:
    session.flush()
    if dry_run:
        session.rollback()
        if result.created:
            result.target_id = None
    else:
        session.commit()
    return result


# --- Debaters -----------------------------------------------------------------------------


def _debater_counts(session: Session, debater_id: int) -> dict[int, dict[str, int]]:
    teams = _grouped(
        session,
        select(Team.tournament_id, func.count(TeamMember.id))
        .join(Team, Team.id == TeamMember.team_id)
        .where(TeamMember.debater_id == debater_id)
        .group_by(Team.tournament_id),
    )
    speeches = _grouped(
        session,
        select(Round.tournament_id, func.count(SpeakerScore.id))
        .join(Ballot, Ballot.id == SpeakerScore.ballot_id)
        .join(Debate, Debate.id == Ballot.debate_id)
        .join(Round, Round.id == Debate.round_id)
        .where(SpeakerScore.debater_id == debater_id)
        .group_by(Round.tournament_id),
    )
    return _merge_counts(teams=teams, speeches=speeches)


def split_info_debater(session: Session, debater_id: int) -> list[SplitTournament]:
    _load(session, Debater, debater_id)
    return _tournament_rows(session, _debater_counts(session, debater_id), ("teams", "speeches"))


def _scores_in(debater_id: int, tournament_ids: set[int]):
    return (
        select(SpeakerScore)
        .join(Ballot, Ballot.id == SpeakerScore.ballot_id)
        .join(Debate, Debate.id == Ballot.debate_id)
        .join(Round, Round.id == Debate.round_id)
        .where(SpeakerScore.debater_id == debater_id, col(Round.tournament_id).in_(tournament_ids))
    )


def split_debater(session: Session, debater_id: int, request: DebaterSplitRequest) -> SplitResult:
    source = _load(session, Debater, debater_id)
    chosen = _validate(debater_id, request, set(_debater_counts(session, debater_id)), "debater")
    target = _load(session, Debater, request.target_id) if request.target_id is not None else None
    if request.new and request.new.institution_id is not None:
        _load(session, Institution, request.new.institution_id)

    scores = session.exec(_scores_in(debater_id, chosen)).all()
    if target is not None:
        target_ballots = set(
            session.exec(select(SpeakerScore.ballot_id).where(SpeakerScore.debater_id == target.id)).all()
        )
        if target_ballots & {s.ballot_id for s in scores}:
            raise MergeConflict(
                f"{_label(target)} has scores on the same ballots, so they spoke as a different person in those debates"
            )
    members = session.exec(
        select(TeamMember)
        .join(Team, Team.id == TeamMember.team_id)
        .where(TeamMember.debater_id == debater_id, col(Team.tournament_id).in_(chosen))
    ).all()

    try:
        created = target is None
        if target is None:
            latest_school = session.exec(
                select(Team.institution_id)
                .join(Tournament, Tournament.id == Team.tournament_id)
                .where(col(Team.id).in_([m.team_id for m in members]), col(Team.institution_id).is_not(None))
                .order_by(col(Tournament.date).desc().nulls_last(), col(Tournament.id).desc())
            ).first()
            target = Debater(**_person_fields(source, request.new, institution_id=latest_school))
            session.add(target)
            session.flush()

        target_teams = set(session.exec(select(TeamMember.team_id).where(TeamMember.debater_id == target.id)).all())
        for member in members:
            if member.team_id in target_teams:
                session.delete(member)
            else:
                member.debater_id = target.id
                target_teams.add(member.team_id)
                session.add(member)
        for score in scores:
            score.debater_id = target.id
            session.add(score)
        for profile in session.exec(
            select(DebaterProfile).where(col(DebaterProfile.debater_id).in_([debater_id, target.id]))
        ).all():
            session.delete(profile)
        result = SplitResult(
            target_id=target.id,
            target_name=_label(target),
            created=created,
            dry_run=request.dry_run,
            moved={"tournaments": len(chosen), "teams": len(members), "speeches": len(scores)},
        )
        return _finish(session, result, request.dry_run)
    except Exception:
        session.rollback()
        raise


# --- Judges -------------------------------------------------------------------------------


def _judge_counts(session: Session, judge_id: int) -> dict[int, dict[str, int]]:
    def by_tournament(model) -> dict[int, int]:
        return _grouped(
            session,
            select(Round.tournament_id, func.count(model.id))
            .join(Debate, Debate.id == model.debate_id)
            .join(Round, Round.id == Debate.round_id)
            .where(model.judge_id == judge_id)
            .group_by(Round.tournament_id),
        )

    return _merge_counts(panels=by_tournament(DebateJudge), ballots=by_tournament(Ballot))


def split_info_judge(session: Session, judge_id: int) -> list[SplitTournament]:
    _load(session, Judge, judge_id)
    return _tournament_rows(session, _judge_counts(session, judge_id), ("panels", "ballots"))


def split_judge(session: Session, judge_id: int, request: JudgeSplitRequest) -> SplitResult:
    source = _load(session, Judge, judge_id)
    chosen = _validate(judge_id, request, set(_judge_counts(session, judge_id)), "judge")
    target = _load(session, Judge, request.target_id) if request.target_id is not None else None
    if request.new and request.new.institution_id is not None:
        _load(session, Institution, request.new.institution_id)

    def rows(model):
        return session.exec(
            select(model)
            .join(Debate, Debate.id == model.debate_id)
            .join(Round, Round.id == Debate.round_id)
            .where(model.judge_id == judge_id, col(Round.tournament_id).in_(chosen))
        ).all()

    panels, ballots = rows(DebateJudge), rows(Ballot)
    if target is not None:
        debates = {p.debate_id for p in panels} | {b.debate_id for b in ballots}
        clash = session.exec(
            select(DebateJudge.id).where(DebateJudge.judge_id == target.id, col(DebateJudge.debate_id).in_(debates))
        ).first() or session.exec(
            select(Ballot.id).where(Ballot.judge_id == target.id, col(Ballot.debate_id).in_(debates))
        ).first()
        if clash is not None:
            raise MergeConflict(f"{_label(target)} sat on the same panels, so they are a different person")

    try:
        created = target is None
        if target is None:
            target = Judge(**_person_fields(source, request.new))
            session.add(target)
            session.flush()
        for row in [*panels, *ballots]:
            row.judge_id = target.id
            session.add(row)
        feedback = session.exec(
            update(Feedback)
            .where(Feedback.judge_id == judge_id, col(Feedback.tournament_id).in_(chosen))
            .values(judge_id=target.id)
        ).rowcount
        for profile in session.exec(
            select(JudgeProfile).where(col(JudgeProfile.judge_id).in_([judge_id, target.id]))
        ).all():
            session.delete(profile)
        result = SplitResult(
            target_id=target.id,
            target_name=_label(target),
            created=created,
            dry_run=request.dry_run,
            moved={"tournaments": len(chosen), "panels": len(panels), "ballots": len(ballots), "feedback": feedback},
        )
        return _finish(session, result, request.dry_run)
    except Exception:
        session.rollback()
        raise


# --- Institutions -------------------------------------------------------------------------


def _institution_counts(session: Session, institution_id: int) -> dict[int, dict[str, int]]:
    teams = _grouped(
        session,
        select(Team.tournament_id, func.count(Team.id))
        .where(Team.institution_id == institution_id)
        .group_by(Team.tournament_id),
    )
    registrations = _grouped(
        session,
        select(TournamentRegistration.tournament_id, func.count(TournamentRegistration.id))
        .where(TournamentRegistration.institution_id == institution_id)
        .group_by(TournamentRegistration.tournament_id),
    )
    return _merge_counts(teams=teams, registrations=registrations)


def split_info_institution(session: Session, institution_id: int) -> list[SplitTournament]:
    _load(session, Institution, institution_id)
    return _tournament_rows(session, _institution_counts(session, institution_id), ("teams", "registrations"))


def _people_moving_school(
    session: Session, institution_id: int, moved_team_ids: set[int], chosen: set[int]
) -> tuple[list[Debater], list[Judge]]:
    """Debaters with this school whose teams are all among the moved ones, and judges with
    this school whose panels are all at the chosen tournaments. Everyone else keeps it."""
    debater_teams: dict[int, set[int]] = defaultdict(set)
    for debater_id, team_id in session.exec(
        select(TeamMember.debater_id, TeamMember.team_id)
        .join(Debater, Debater.id == TeamMember.debater_id)
        .where(Debater.institution_id == institution_id)
    ).all():
        debater_teams[debater_id].add(team_id)
    debater_ids = [d for d, teams in debater_teams.items() if teams <= moved_team_ids]

    judge_tournaments: dict[int, set[int]] = defaultdict(set)
    for judge_id, tournament_id in session.exec(
        select(DebateJudge.judge_id, Round.tournament_id)
        .join(Judge, Judge.id == DebateJudge.judge_id)
        .join(Debate, Debate.id == DebateJudge.debate_id)
        .join(Round, Round.id == Debate.round_id)
        .where(Judge.institution_id == institution_id)
        .distinct()
    ).all():
        judge_tournaments[judge_id].add(tournament_id)
    judge_ids = [j for j, tournaments in judge_tournaments.items() if tournaments <= chosen]

    debaters = session.exec(select(Debater).where(col(Debater.id).in_(debater_ids)).order_by(Debater.id)).all() if debater_ids else []
    judges = session.exec(select(Judge).where(col(Judge.id).in_(judge_ids)).order_by(Judge.id)).all() if judge_ids else []
    return list(debaters), list(judges)


def split_institution(session: Session, institution_id: int, request: InstitutionSplitRequest) -> SplitResult:
    _load(session, Institution, institution_id)
    chosen = _validate(institution_id, request, set(_institution_counts(session, institution_id)), "school")
    target = _load(session, Institution, request.target_id) if request.target_id is not None else None
    if target is None and request.new is None:
        raise MergeInvalid("Give the new school a name")

    registrations = session.exec(
        select(TournamentRegistration).where(
            TournamentRegistration.institution_id == institution_id,
            col(TournamentRegistration.tournament_id).in_(chosen),
        )
    ).all()
    if target is not None and registrations:
        taken = session.exec(
            select(TournamentRegistration.tournament_id).where(
                TournamentRegistration.institution_id == target.id,
                col(TournamentRegistration.tournament_id).in_([r.tournament_id for r in registrations]),
            )
        ).first()
        if taken is not None:
            raise MergeConflict(
                f"{target.name} is already registered for tournament {taken}; delete or edit one registration first"
            )
    teams = session.exec(
        select(Team).where(Team.institution_id == institution_id, col(Team.tournament_id).in_(chosen))
    ).all()
    debaters, judges = _people_moving_school(session, institution_id, {t.id for t in teams}, chosen)

    try:
        created = target is None
        if target is None:
            target = Institution(**request.new.model_dump())
            session.add(target)
            try:
                session.flush()
            except IntegrityError as exc:
                session.rollback()
                raise MergeConflict(f"A school named {request.new.name!r} already exists; split into it instead") from exc
        for row in [*teams, *registrations, *debaters, *judges]:
            row.institution_id = target.id
            session.add(row)
        moved = {"tournaments": len(chosen), "teams": len(teams), "registrations": len(registrations)}
        for model, key in ((Transaction, "transactions"), (Feedback, "feedback"), (SchoolAchievement, "achievements")):
            moved[key] = session.exec(
                update(model)
                .where(model.institution_id == institution_id, col(model.tournament_id).in_(chosen))
                .values(institution_id=target.id)
            ).rowcount
        result = SplitResult(
            target_id=target.id,
            target_name=target.name,
            created=created,
            dry_run=request.dry_run,
            moved=moved,
            school_moves=[f"Debater {_label(d)}" for d in debaters] + [f"Judge {_label(j)}" for j in judges],
        )
        return _finish(session, result, request.dry_run)
    except Exception:
        session.rollback()
        raise
