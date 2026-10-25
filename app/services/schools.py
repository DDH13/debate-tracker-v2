"""Member-school views for the management console: membership standing per year, the
schools list, and one school's full record.

Pure reads. Every query covers all schools at once (one per table), so the list stays a
handful of queries however many institutions there are.
"""

from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from enum import Enum
from typing import Literal

from sqlmodel import Session, SQLModel, func, select

from app.models import (
    ContactRole,
    Feedback,
    FeedbackPublic,
    FeedbackStatus,
    Institution,
    InstitutionKind,
    InstitutionPublic,
    Membership,
    MembershipPublic,
    OutreachLead,
    OutreachLeadPublic,
    SchoolAchievement,
    SchoolAchievementPublic,
    SchoolContact,
    SchoolContactPublic,
    SchoolParticipation,
    Team,
    Tournament,
)
from app.services.workshops import SchoolWorkshop, school_workshops


class MembershipStatus(str, Enum):
    MEMBER = "member"
    LAPSED = "lapsed"
    FORMER = "former"
    NEVER = "never"


class MembershipStanding(SQLModel):
    status: MembershipStatus
    # Missed at least one year between their first membership and the reference year.
    irregular: bool
    member_years: list[int]
    first_year: int | None
    last_year: int | None
    # Consecutive member years ending at the reference year (0 unless a member that year).
    streak: int


class TournamentRef(SQLModel):
    id: int
    name: str
    date: date | None


class SchoolSummary(SQLModel):
    institution_id: int
    name: str
    code: str | None
    kind: InstitutionKind | None
    district: str | None
    standing: MembershipStanding
    membership: MembershipPublic | None
    tournament_count: int
    latest_tournament: TournamentRef | None
    primary_contact: SchoolContactPublic | None
    open_feedback: int


class HistoryEntry(SQLModel):
    source: Literal["imported", "manual"]
    tournament_id: int | None
    participation_id: int | None
    event_name: str
    year: int | None
    date: date | None
    teams: int | None
    notes: str | None


class SchoolDetail(SQLModel):
    institution: InstitutionPublic
    summary: SchoolSummary
    memberships: list[MembershipPublic]
    contacts: list[SchoolContactPublic]
    achievements: list[SchoolAchievementPublic]
    history: list[HistoryEntry]
    feedback: list[FeedbackPublic]
    outreach: list[OutreachLeadPublic]
    workshops: list[SchoolWorkshop]


def membership_standing(years: Iterable[int], year: int) -> MembershipStanding:
    """Where a school stands in `year`, given the years it was a member. Years after `year`
    (memberships recorded in advance) don't count towards the status."""
    member_years = sorted(set(years))
    counted = [y for y in member_years if y <= year]
    held = set(counted)

    if year in held:
        status = MembershipStatus.MEMBER
    elif year - 1 in held:
        status = MembershipStatus.LAPSED
    elif counted:
        status = MembershipStatus.FORMER
    else:
        status = MembershipStatus.NEVER

    irregular = bool(counted) and any(
        y not in held for y in range(counted[0], min(counted[-1], year) + 1)
    )

    streak = 0
    while year - streak in held:
        streak += 1

    return MembershipStanding(
        status=status,
        irregular=irregular,
        member_years=member_years,
        first_year=member_years[0] if member_years else None,
        last_year=member_years[-1] if member_years else None,
        streak=streak,
    )


def _primary_contact_rank(contact: SchoolContact) -> tuple[int, int, int]:
    return (
        0 if contact.is_primary else 1,
        0 if contact.role == ContactRole.POINT_OF_CONTACT else 1,
        contact.id or 0,
    )


def _tournament_sort_key(ref: TournamentRef) -> tuple[bool, date, int]:
    # Latest first; undated tournaments sort after every dated one.
    return (ref.date is not None, ref.date or date.min, ref.id)


def school_summaries(
    session: Session, year: int, institution_id: int | None = None
) -> list[SchoolSummary]:
    """One summary per institution (or just `institution_id`), sorted by name."""

    def scoped(statement, column):
        return statement if institution_id is None else statement.where(column == institution_id)

    institutions = session.exec(
        scoped(select(Institution), Institution.id).order_by(Institution.name)
    ).all()

    memberships: dict[int, list[Membership]] = defaultdict(list)
    for membership in session.exec(scoped(select(Membership), Membership.institution_id)).all():
        memberships[membership.institution_id].append(membership)

    tournaments: dict[int, list[TournamentRef]] = defaultdict(list)
    pairs = session.exec(
        scoped(
            select(Team.institution_id, Tournament.id, Tournament.name, Tournament.date)
            .join(Tournament, Tournament.id == Team.tournament_id)
            .where(Team.institution_id.is_not(None))
            .distinct(),
            Team.institution_id,
        )
    ).all()
    for inst_id, t_id, t_name, t_date in pairs:
        tournaments[inst_id].append(TournamentRef(id=t_id, name=t_name, date=t_date))

    contacts: dict[int, SchoolContact] = {}
    for contact in session.exec(
        scoped(select(SchoolContact).where(SchoolContact.active), SchoolContact.institution_id)
    ).all():
        current = contacts.get(contact.institution_id)
        if current is None or _primary_contact_rank(contact) < _primary_contact_rank(current):
            contacts[contact.institution_id] = contact

    open_feedback = dict(
        session.exec(
            scoped(
                select(Feedback.institution_id, func.count())
                .where(Feedback.status == FeedbackStatus.OPEN, Feedback.institution_id.is_not(None))
                .group_by(Feedback.institution_id),
                Feedback.institution_id,
            )
        ).all()
    )

    summaries = []
    for institution in institutions:
        held = memberships.get(institution.id, [])
        attended = tournaments.get(institution.id, [])
        this_year = next((m for m in held if m.year == year), None)
        contact = contacts.get(institution.id)
        summaries.append(
            SchoolSummary(
                institution_id=institution.id,
                name=institution.name,
                code=institution.code,
                kind=institution.kind,
                district=institution.district,
                standing=membership_standing((m.year for m in held), year),
                membership=MembershipPublic.model_validate(this_year) if this_year else None,
                tournament_count=len(attended),
                latest_tournament=max(attended, key=_tournament_sort_key) if attended else None,
                primary_contact=SchoolContactPublic.model_validate(contact) if contact else None,
                open_feedback=open_feedback.get(institution.id, 0),
            )
        )
    return summaries


def institution_tournament_history(session: Session, institution_id: int) -> list[HistoryEntry]:
    """Imported tournaments (from the school's teams) and manually recorded ones, newest
    first; entries with no year go last."""
    imported = session.exec(
        select(Tournament.id, Tournament.name, Tournament.date, func.count(Team.id))
        .join(Team, Team.tournament_id == Tournament.id)
        .where(Team.institution_id == institution_id)
        .group_by(Tournament.id, Tournament.name, Tournament.date)
    ).all()
    entries = [
        HistoryEntry(
            source="imported",
            tournament_id=t_id,
            participation_id=None,
            event_name=t_name,
            year=t_date.year if t_date else None,
            date=t_date,
            teams=team_count,
            notes=None,
        )
        for t_id, t_name, t_date, team_count in imported
    ]
    for row in session.exec(
        select(SchoolParticipation).where(SchoolParticipation.institution_id == institution_id)
    ).all():
        entries.append(
            HistoryEntry(
                source="manual",
                tournament_id=None,
                participation_id=row.id,
                event_name=row.event_name,
                year=row.year,
                date=None,
                teams=row.teams,
                notes=row.notes,
            )
        )
    entries.sort(
        key=lambda e: (e.year is not None, e.year or 0, e.date or date.min, e.event_name),
        reverse=True,
    )
    return entries


def school_detail(session: Session, institution: Institution, year: int) -> SchoolDetail:
    institution_id = institution.id

    def rows(model, *order_by):
        return session.exec(
            select(model).where(model.institution_id == institution_id).order_by(*order_by)
        ).all()

    return SchoolDetail(
        institution=InstitutionPublic.model_validate(institution),
        summary=school_summaries(session, year, institution_id)[0],
        memberships=[
            MembershipPublic.model_validate(m) for m in rows(Membership, Membership.year.desc())
        ],
        contacts=[
            SchoolContactPublic.model_validate(c)
            for c in rows(
                SchoolContact,
                SchoolContact.active.desc(),
                SchoolContact.is_primary.desc(),
                SchoolContact.name,
            )
        ],
        achievements=[
            SchoolAchievementPublic.model_validate(a)
            for a in rows(SchoolAchievement, SchoolAchievement.year.desc(), SchoolAchievement.id)
        ],
        history=institution_tournament_history(session, institution_id),
        feedback=[
            FeedbackPublic.model_validate(f)
            for f in rows(Feedback, Feedback.created_at.desc(), Feedback.id.desc())
        ],
        outreach=[
            OutreachLeadPublic.model_validate(lead)
            for lead in rows(OutreachLead, OutreachLead.created_at.desc())
        ],
        workshops=school_workshops(session, institution_id),
    )
