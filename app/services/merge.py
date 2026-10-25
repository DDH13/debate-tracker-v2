"""Merge duplicate debaters, judges and institutions into a single surviving row.

Every merge validates first, mutates, then commits once, so a failure leaves the database
untouched. Services don't raise `HTTPException`; the routers map `MergeError` subclasses to
404 / 422 / 409.
"""

from collections import defaultdict
from datetime import date

from sqlmodel import Session, SQLModel, select, update

from app.models import (
    Ballot,
    DebateJudge,
    Debater,
    DebaterProfile,
    Feedback,
    Institution,
    Judge,
    JudgeProfile,
    Membership,
    OutreachLead,
    SchoolAchievement,
    SchoolContact,
    SchoolParticipation,
    SpeakerScore,
    SquadMember,
    Team,
    TeamMember,
    Tournament,
    Transaction,
    WorkshopSchool,
)
from app.services.stats import core
from app.services.stats import judge as judge_stats
from app.services.text import name_key

_PERSON_FIELDS = (
    "full_name",
    "first_name",
    "last_name",
    "email",
    "phone",
    "birthdate",
    "gender",
    "institution_id",
)


class MergeError(Exception):
    pass


class MergeNotFound(MergeError):
    """A referenced row does not exist (404)."""


class MergeInvalid(MergeError):
    """The request itself is malformed, e.g. merging a row into itself (422)."""


class MergeConflict(MergeError):
    """The rows are provably distinct and cannot be merged (409)."""


def _load(session: Session, model: type, id_: int):
    obj = session.get(model, id_)
    if obj is None:
        raise MergeNotFound(f"{model.__name__} {id_} not found")
    return obj


def _fill_null_fields(target, source, fields: tuple[str, ...]) -> None:
    for field in fields:
        if getattr(target, field) is None and getattr(source, field) is not None:
            setattr(target, field, getattr(source, field))


def merge_debaters(session: Session, source_id: int, target_id: int) -> Debater:
    if source_id == target_id:
        raise MergeInvalid("source_id and target_id must differ")
    source = _load(session, Debater, source_id)
    target = _load(session, Debater, target_id)

    source_ballots = set(
        session.exec(select(SpeakerScore.ballot_id).where(SpeakerScore.debater_id == source_id)).all()
    )
    target_ballots = set(
        session.exec(select(SpeakerScore.ballot_id).where(SpeakerScore.debater_id == target_id)).all()
    )
    if source_ballots & target_ballots:
        raise MergeConflict(
            f"Debaters {source_id} and {target_id} both have scores on the same ballot, "
            f"so they spoke as different people in one debate"
        )

    try:
        target_team_ids = set(
            session.exec(select(TeamMember.team_id).where(TeamMember.debater_id == target_id)).all()
        )
        session.exec(
            update(SpeakerScore).where(SpeakerScore.debater_id == source_id).values(debater_id=target_id)
        )
        for member in session.exec(select(TeamMember).where(TeamMember.debater_id == source_id)).all():
            if member.team_id in target_team_ids:
                session.delete(member)
            else:
                member.debater_id = target_id
                target_team_ids.add(member.team_id)
                session.add(member)

        target_squad_ids = set(
            session.exec(select(SquadMember.squad_id).where(SquadMember.debater_id == target_id)).all()
        )
        for squad_member in session.exec(select(SquadMember).where(SquadMember.debater_id == source_id)).all():
            if squad_member.squad_id in target_squad_ids:
                session.delete(squad_member)
            else:
                squad_member.debater_id = target_id
                target_squad_ids.add(squad_member.squad_id)
                session.add(squad_member)

        _fill_null_fields(target, source, _PERSON_FIELDS)
        session.add(target)

        for profile in session.exec(
            select(DebaterProfile).where(DebaterProfile.debater_id == source_id)
        ).all():
            session.delete(profile)
        session.flush()
        # Drop stale relationship collections so deleting the source can't cascade onto
        # rows that were just moved to the target.
        session.expire(source)
        session.delete(source)
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(target)
    return target


def merge_judges(session: Session, source_id: int, target_id: int) -> Judge:
    if source_id == target_id:
        raise MergeInvalid("source_id and target_id must differ")
    source = _load(session, Judge, source_id)
    target = _load(session, Judge, target_id)

    def debate_ids(model, judge_id: int) -> set[int]:
        return set(session.exec(select(model.debate_id).where(model.judge_id == judge_id)).all())

    if debate_ids(DebateJudge, source_id) & debate_ids(DebateJudge, target_id):
        raise MergeConflict(
            f"Judges {source_id} and {target_id} sat on the same panel, so they are different people"
        )
    if debate_ids(Ballot, source_id) & debate_ids(Ballot, target_id):
        raise MergeConflict(
            f"Judges {source_id} and {target_id} both submitted a ballot for the same debate"
        )

    try:
        session.exec(update(Ballot).where(Ballot.judge_id == source_id).values(judge_id=target_id))
        session.exec(
            update(DebateJudge).where(DebateJudge.judge_id == source_id).values(judge_id=target_id)
        )
        session.exec(update(Feedback).where(Feedback.judge_id == source_id).values(judge_id=target_id))
        _fill_null_fields(target, source, _PERSON_FIELDS)
        session.add(target)
        for profile in session.exec(select(JudgeProfile).where(JudgeProfile.judge_id == source_id)).all():
            session.delete(profile)
        session.flush()
        session.expire(source)
        session.delete(source)
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(target)
    return target


def _merge_workshop_attendance(session: Session, target_id: int, source_ids: list[int]) -> None:
    """Moves the sources' workshop attendance to the target. When several of the merged
    schools attended the same workshop (one row per school per workshop), keep one row with
    the students added up and the notes joined."""
    rows = session.exec(
        select(WorkshopSchool)
        .where(WorkshopSchool.institution_id.in_([target_id, *source_ids]))
        .order_by(WorkshopSchool.institution_id != target_id, WorkshopSchool.id)
    ).all()
    kept: dict[int, WorkshopSchool] = {}
    for row in rows:
        keep = kept.get(row.workshop_id)
        if keep is None:
            row.institution_id = target_id
            kept[row.workshop_id] = row
            session.add(row)
            continue
        if row.students is not None:
            keep.students = (keep.students or 0) + row.students
        keep.notes = "\n".join(n for n in (keep.notes, row.notes) if n) or None
        session.add(keep)
        session.delete(row)
    session.flush()


def merge_institutions(session: Session, target_id: int, source_ids: list[int]) -> Institution:
    unique_sources = list(dict.fromkeys(source_ids))
    if not unique_sources:
        raise MergeInvalid("source_ids must not be empty")
    if target_id in unique_sources:
        raise MergeInvalid("target_id must not appear in source_ids")
    target = _load(session, Institution, target_id)
    sources = [_load(session, Institution, source_id) for source_id in unique_sources]

    # Two membership rows for the same year can't both survive (one per school per year),
    # and silently dropping one would lose fee/payment details, so refuse instead.
    seen_years: dict[int, int] = {}
    for membership in session.exec(
        select(Membership).where(Membership.institution_id.in_([target_id, *unique_sources]))
    ).all():
        other = seen_years.setdefault(membership.year, membership.institution_id)
        if other != membership.institution_id:
            raise MergeConflict(
                f"Institutions {other} and {membership.institution_id} both have a "
                f"{membership.year} membership; delete or edit one before merging"
            )

    try:
        _merge_workshop_attendance(session, target_id, unique_sources)
        for model in (
            Team,
            Debater,
            Judge,
            Membership,
            SchoolContact,
            SchoolAchievement,
            SchoolParticipation,
            Feedback,
            OutreachLead,
            Transaction,
        ):
            session.exec(
                update(model)
                .where(model.institution_id.in_(unique_sources))
                .values(institution_id=target_id)
            )
        for source in sources:
            _fill_null_fields(target, source, ("code", "kind", "district"))
        session.add(target)
        session.flush()
        for source in sources:
            session.expire(source)
            session.delete(source)
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(target)
    return target


# --- Duplicate finders and merge-info lists (read-only admin views) ---


class DuplicatePerson(SQLModel):
    id: int
    full_name: str | None
    first_name: str | None
    last_name: str | None
    email: str | None
    birthdate: date | None
    institution_id: int | None


class DuplicateGroup(SQLModel):
    key: str
    members: list[DuplicatePerson]


class TeamRef(SQLModel):
    team_id: int
    name: str
    tournament_abbr: str | None


class DebaterMergeInfo(SQLModel):
    id: int
    full_name: str | None
    first_name: str | None
    last_name: str | None
    phone: str | None
    institution_id: int | None
    institution_name: str | None
    debates: int
    teams: list[TeamRef]


class JudgeMergeInfo(SQLModel):
    id: int
    full_name: str | None
    first_name: str | None
    last_name: str | None
    institution_id: int | None
    institution_name: str | None
    prelims_judged: int
    elims_judged: int
    tournaments: list[str]


class InstitutionMergeInfo(SQLModel):
    id: int
    name: str
    code: str | None
    team_count: int
    team_names: list[str]


def _person_sort_key(person) -> tuple[str, int]:
    return (core.display_name(person.full_name, person.first_name, person.last_name).casefold(), person.id)


def find_duplicates(
    session: Session, model: type[Debater] | type[Judge], *, by_birthdate: bool = False
) -> list[DuplicateGroup]:
    """Groups of people sharing a `name_key` (and, with `by_birthdate`, the same non-null
    birthdate). Only groups of two or more are returned."""
    groups: dict[str, list] = defaultdict(list)
    for person in session.exec(select(model).order_by(model.id)).all():
        key = name_key(core.display_name(person.full_name, person.first_name, person.last_name))
        if not key:
            continue
        if by_birthdate:
            if person.birthdate is None:
                continue
            key = f"{key}|{person.birthdate.isoformat()}"
        groups[key].append(person)

    return [
        DuplicateGroup(
            key=key,
            members=[
                DuplicatePerson.model_validate(p, from_attributes=True)
                for p in sorted(members, key=_person_sort_key)
            ],
        )
        for key, members in sorted(groups.items())
        if len(members) > 1
    ]


def _institution_names(session: Session) -> dict[int, str]:
    return {i.id: i.name for i in session.exec(select(Institution)).all()}


def debater_merge_info(session: Session) -> list[DebaterMergeInfo]:
    debates_by_debater: dict[int, set[int]] = defaultdict(set)
    for debater_id, debate_id in session.exec(
        select(SpeakerScore.debater_id, Ballot.debate_id)
        .join(Ballot, SpeakerScore.ballot_id == Ballot.id)
        .where(
            Ballot.discarded == False,  # noqa: E712
            Ballot.forfeit == False,  # noqa: E712
        )
    ).all():
        debates_by_debater[debater_id].add(debate_id)

    teams_by_debater: dict[int, list[TeamRef]] = defaultdict(list)
    for debater_id, team, tournament in session.exec(
        select(TeamMember.debater_id, Team, Tournament)
        .join(Team, TeamMember.team_id == Team.id)
        .join(Tournament, Team.tournament_id == Tournament.id)
        .order_by(Tournament.date, Team.id)
    ).all():
        teams_by_debater[debater_id].append(
            TeamRef(team_id=team.id, name=team.name, tournament_abbr=tournament.abbr)
        )

    institutions = _institution_names(session)
    debaters = sorted(session.exec(select(Debater)).all(), key=_person_sort_key)
    return [
        DebaterMergeInfo(
            id=d.id,
            full_name=d.full_name,
            first_name=d.first_name,
            last_name=d.last_name,
            phone=d.phone,
            institution_id=d.institution_id,
            institution_name=institutions.get(d.institution_id),
            debates=len(debates_by_debater.get(d.id, ())),
            teams=teams_by_debater.get(d.id, []),
        )
        for d in debaters
    ]


def judge_merge_info(session: Session) -> list[JudgeMergeInfo]:
    aggregates = judge_stats.compute_judge_aggregates(session)
    tournament_names = {t.id: t.name for t in session.exec(select(Tournament)).all()}
    institutions = _institution_names(session)
    judges = sorted(session.exec(select(Judge)).all(), key=_person_sort_key)
    results = []
    for judge in judges:
        agg = aggregates.get(judge.id)
        results.append(
            JudgeMergeInfo(
                id=judge.id,
                full_name=judge.full_name,
                first_name=judge.first_name,
                last_name=judge.last_name,
                institution_id=judge.institution_id,
                institution_name=institutions.get(judge.institution_id),
                prelims_judged=agg.prelims_judged if agg else 0,
                elims_judged=agg.elims_judged if agg else 0,
                tournaments=sorted(tournament_names[t] for t in agg.tournament_ids) if agg else [],
            )
        )
    return results


def institution_merge_info(session: Session) -> list[InstitutionMergeInfo]:
    team_names: dict[int, list[str]] = defaultdict(list)
    for institution_id, name in session.exec(
        select(Team.institution_id, Team.name)
        .where(Team.institution_id.is_not(None))
        .order_by(Team.name)
    ).all():
        team_names[institution_id].append(name)
    institutions = session.exec(select(Institution).order_by(Institution.name)).all()
    return [
        InstitutionMergeInfo(
            id=i.id,
            name=i.name,
            code=i.code,
            team_count=len(team_names.get(i.id, ())),
            team_names=team_names.get(i.id, []),
        )
        for i in institutions
    ]
