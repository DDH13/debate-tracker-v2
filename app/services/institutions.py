"""Institution identity helpers: exact key matching for imports, fuzzy search for humans."""

from sqlmodel import Session, SQLModel, select

from app.models import Institution
from app.services.text import institution_key, jaro_winkler, normalize_text


class SimilarInstitution(SQLModel):
    id: int
    name: str
    code: str | None
    score: float


def find_matching_institution(session: Session, name: str) -> Institution | None:
    """The existing institution whose `institution_key` equals the incoming name's, if any.
    Deliberately key-equality only: fuzzy (Jaro-Winkler) matches are never auto-applied."""
    key = institution_key(name)
    if not key:
        return None
    for institution in session.exec(select(Institution).order_by(Institution.id)).all():
        if institution_key(institution.name) == key:
            return institution
    return None


def similar_institutions(
    session: Session, name: str, threshold: float = 0.8
) -> list[SimilarInstitution]:
    """Institutions with Jaro-Winkler >= `threshold` against `name`, or one containing the
    other, best match first."""
    query = normalize_text(name).lower()
    if not query:
        return []
    results: list[SimilarInstitution] = []
    for institution in session.exec(select(Institution)).all():
        candidate = normalize_text(institution.name).lower()
        score = jaro_winkler(query, candidate)
        if score >= threshold or query in candidate or candidate in query:
            results.append(
                SimilarInstitution(
                    id=institution.id,
                    name=institution.name,
                    code=institution.code,
                    score=round(score, 4),
                )
            )
    results.sort(key=lambda r: (-r.score, r.name))
    return results
