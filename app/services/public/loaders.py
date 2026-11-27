"""Batched reference lookups. One query per kind however many ids are asked for."""

from collections.abc import Iterable

from sqlmodel import Session, select

from app.models import Debater, Institution, Judge, Round, Team, Tournament
from app.services.public.schemas import InstitutionRef, PersonRef, RoundRef, TeamRef, TournamentRef
from app.services.stats import core


def person_name(obj: Debater | Judge, label: str) -> str:
    return core.display_name(obj.full_name, obj.first_name, obj.last_name) or f"{label} {obj.id}"


def _ids(ids: Iterable[int | None]) -> list[int]:
    return sorted({i for i in ids if i is not None})


def institution_ref(inst: Institution) -> InstitutionRef:
    return InstitutionRef(id=inst.id, name=inst.name, code=inst.code, kind=inst.kind)


def institution_refs(session: Session, ids: Iterable[int | None]) -> dict[int, InstitutionRef]:
    wanted = _ids(ids)
    if not wanted:
        return {}
    return {
        i.id: institution_ref(i)
        for i in session.exec(select(Institution).where(Institution.id.in_(wanted))).all()
    }


def debater_refs(session: Session, ids: Iterable[int | None]) -> dict[int, PersonRef]:
    wanted = _ids(ids)
    if not wanted:
        return {}
    return {
        d.id: PersonRef(id=d.id, name=person_name(d, "Debater"))
        for d in session.exec(select(Debater).where(Debater.id.in_(wanted))).all()
    }


def judge_refs(session: Session, ids: Iterable[int | None]) -> dict[int, PersonRef]:
    wanted = _ids(ids)
    if not wanted:
        return {}
    return {
        j.id: PersonRef(id=j.id, name=person_name(j, "Judge"))
        for j in session.exec(select(Judge).where(Judge.id.in_(wanted))).all()
    }


def team_refs(session: Session, ids: Iterable[int | None]) -> dict[int, TeamRef]:
    wanted = _ids(ids)
    if not wanted:
        return {}
    teams = session.exec(select(Team).where(Team.id.in_(wanted))).all()
    institutions = institution_refs(session, (t.institution_id for t in teams))
    return {
        t.id: TeamRef(id=t.id, name=t.name, institution=institutions.get(t.institution_id))
        for t in teams
    }


def tournament_ref(t: Tournament) -> TournamentRef:
    return TournamentRef(id=t.id, name=t.name, abbr=t.abbr, slug=t.slug, date=t.date)


def tournament_refs(session: Session, ids: Iterable[int | None]) -> dict[int, TournamentRef]:
    wanted = _ids(ids)
    if not wanted:
        return {}
    return {
        t.id: tournament_ref(t)
        for t in session.exec(select(Tournament).where(Tournament.id.in_(wanted))).all()
    }


def round_ref(r: Round) -> RoundRef:
    return RoundRef(id=r.id, seq=r.seq, name=r.name, abbr=r.abbr, is_elimination=r.isElimination)


def round_refs(session: Session, ids: Iterable[int | None]) -> dict[int, RoundRef]:
    wanted = _ids(ids)
    if not wanted:
        return {}
    return {r.id: round_ref(r) for r in session.exec(select(Round).where(Round.id.in_(wanted))).all()}
