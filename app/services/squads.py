"""A squad's roster: members with their debater and school names, and staff, in one read."""

from datetime import date

from sqlmodel import Session, SQLModel, select

from app.models import Debater, Institution, Squad, SquadKind, SquadMember, SquadRole, SquadStaff
from app.services.stats.core import display_name


class RosterMember(SQLModel):
    id: int
    debater_id: int
    debater_name: str
    institution_id: int | None
    school_name: str | None
    role: SquadRole
    joined_on: date | None
    left_on: date | None
    notes: str | None


class RosterStaff(SQLModel):
    id: int
    name: str
    role: str | None
    person_id: int | None
    notes: str | None


class SquadRoster(SQLModel):
    id: int
    kind: SquadKind
    season: str
    notes: str | None
    members: list[RosterMember]
    staff: list[RosterStaff]


_ROLE_ORDER = {SquadRole.CAPTAIN: 0, SquadRole.MEMBER: 1, SquadRole.RESERVE: 2}


def squad_roster(session: Session, squad: Squad) -> SquadRoster:
    rows = session.exec(
        select(SquadMember, Debater, Institution)
        .join(Debater, Debater.id == SquadMember.debater_id)
        .join(Institution, Institution.id == Debater.institution_id, isouter=True)
        .where(SquadMember.squad_id == squad.id)
    ).all()
    members = [
        RosterMember(
            id=m.id,
            debater_id=d.id,
            debater_name=display_name(d.full_name, d.first_name, d.last_name),
            institution_id=d.institution_id,
            school_name=i.name if i else None,
            role=m.role,
            joined_on=m.joined_on,
            left_on=m.left_on,
            notes=m.notes,
        )
        for m, d, i in rows
    ]
    members.sort(key=lambda m: (_ROLE_ORDER[m.role], m.debater_name.lower(), m.id))
    staff = session.exec(select(SquadStaff).where(SquadStaff.squad_id == squad.id).order_by(SquadStaff.id)).all()
    return SquadRoster(
        id=squad.id,
        kind=squad.kind,
        season=squad.season,
        notes=squad.notes,
        members=members,
        staff=[RosterStaff(id=s.id, name=s.name, role=s.role, person_id=s.person_id, notes=s.notes) for s in staff],
    )
