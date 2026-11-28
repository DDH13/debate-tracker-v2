"""Institution identity helpers: exact key matching for imports, fuzzy search for humans."""

from typing import Literal

from sqlmodel import Session, SQLModel, select

from app.models import Institution
from app.services.text import institution_key, jaro_winkler, normalize_text, school_tokens

SchoolRelation = Literal["same", "subset", "different"]
# Two school words are a spelling variant of each other at this Jaro-Winkler score, provided they
# start with the same letter (so "nalanda" is never "ananda") and are at least 4 letters long.
_WORD_VARIANT_SCORE = 0.92


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


def _same_word(a: str, b: str) -> bool:
    if a == b:
        return True
    return min(len(a), len(b)) >= 4 and a[0] == b[0] and jaro_winkler(a, b) >= _WORD_VARIANT_SCORE


def school_relation(a: str, b: str) -> SchoolRelation:
    """How two school names relate, word by word (see `school_tokens`):

    - `same`: every word pairs up with an equal word or a spelling variant ("Visakha" /
      "Vishaka", "St Thomas'" / "S. Thomas'").
    - `subset`: one name is the other plus extra words **at the end** ("Hillwood College" /
      "Hillwood College Kandy"); the shorter one may just leave out the branch or town, so it's
      for a human to say.
    - `different`: anything else. Each name has a word the other lacks ("Gateway College
      Negombo" / "Gateway College Dehiwala": branches), or the extra words come first or in the
      middle ("Ladies' College" / "Muslim Ladies' College": a different school).
    """
    left, right = school_tokens(a), school_tokens(b)
    if not left or not right:
        return "same" if institution_key(a) == institution_key(b) else "different"
    unmatched = list(range(len(right)))
    left_hits: list[bool] = []
    for word in left:
        hit = next((j for j in unmatched if right[j] == word), None)
        if hit is None:
            hit = next((j for j in unmatched if _same_word(word, right[j])), None)
        left_hits.append(hit is not None)
        if hit is not None:
            unmatched.remove(hit)
    right_hits = [j not in unmatched for j in range(len(right))]
    extra_left, extra_right = not all(left_hits), bool(unmatched)
    if not extra_left and not extra_right:
        return "same"
    if extra_left and extra_right:
        return "different"
    hits = left_hits if extra_left else right_hits
    # The shared words must all come first: the extras are a trailing run.
    first_extra = hits.index(False)
    return "subset" if not any(hits[first_extra:]) else "different"


def similar_institutions(
    session: Session, name: str, threshold: float = 0.8
) -> list[SimilarInstitution]:
    """Institutions with Jaro-Winkler >= `threshold` against `name`, one containing the other,
    or the same distinguishing words (`school_relation` "same", which catches "The British School
    in Colombo" for "British School of Colombo"), best match first."""
    query = normalize_text(name).lower()
    if not query:
        return []
    results: list[SimilarInstitution] = []
    for institution in session.exec(select(Institution)).all():
        candidate = normalize_text(institution.name).lower()
        score = jaro_winkler(query, candidate)
        if (
            score >= threshold
            or query in candidate
            or candidate in query
            or school_relation(name, institution.name) == "same"
        ):
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
