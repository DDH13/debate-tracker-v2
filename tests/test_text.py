import pytest
from sqlmodel import Session

from app.models import Institution
from app.services.institutions import find_matching_institution, similar_institutions
from app.services.text import (
    institution_key,
    jaro_winkler,
    name_key,
    normalize_text,
    split_name,
    tidy_case,
)


# --- normalize_text / name_key ---


def test_normalize_text_strips_html_and_unescapes_entities() -> None:
    assert normalize_text("<p>This House &amp; that <b>one</b></p>") == "This House & that one"


def test_normalize_text_maps_exotic_spaces_quotes_and_dashes() -> None:
    raw = "St Peter’s College – “Old” Boys — Club"
    assert normalize_text(raw) == "St Peter's College - \"Old\" Boys - Club"


def test_normalize_text_nfkc_and_whitespace_collapse() -> None:
    assert normalize_text("  ﬁne \n\t  print  ") == "fine print"


def test_normalize_text_handles_none_and_empty() -> None:
    assert normalize_text(None) == ""
    assert normalize_text("") == ""


def test_name_key_is_case_and_whitespace_insensitive() -> None:
    assert name_key("  JOHN   Smith ") == name_key("john smith")


# --- split_name ---


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Alice Adams", ("Alice", "Adams")),
        ("Juan de la Cruz", ("Juan", "la Cruz")),
        ("Anna van Berg", ("Anna", "van Berg")),
        ("Mary Jane Watson", ("Mary", "Watson")),
        ("J. K. Rowling", ("J", "Rowling")),
        ("Madonna", ("Madonna", None)),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_split_name(name: str | None, expected: tuple[str | None, str | None]) -> None:
    assert split_name(name) == expected


# --- tidy_case ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("john smith", "John Smith"),
        ("JOHN SMITH", "John Smith"),
        ("anna van berg", "Anna van Berg"),
        ("JUAN VON TRAPP", "Juan von Trapp"),
        ("mary-jane watson", "Mary-Jane Watson"),
        ("McDonald", "McDonald"),
        ("John smith", "John smith"),  # mixed case is never touched
        ("", ""),
    ],
)
def test_tidy_case(raw: str, expected: str) -> None:
    assert tidy_case(raw) == expected


# --- institution_key ---


def test_institution_key_ignores_case_punctuation_and_spacing() -> None:
    assert institution_key("St. Peter's College") == institution_key("St Peters College")
    assert institution_key("St. Peter's College") == "stpeterscollege"
    assert institution_key("  ROYAL  college (Colombo) ") == "royalcollegecolombo"


def test_institution_key_distinguishes_different_names() -> None:
    assert institution_key("Royal College") != institution_key("Royal Institute")


# --- jaro_winkler ---


def test_jaro_winkler_matches_known_values() -> None:
    assert jaro_winkler("MARTHA", "MARHTA") == pytest.approx(0.961, abs=1e-3)
    assert jaro_winkler("DIXON", "DICKSONX") == pytest.approx(0.813, abs=1e-3)


def test_jaro_winkler_edge_cases() -> None:
    assert jaro_winkler("same", "same") == 1.0
    assert jaro_winkler("", "x") == 0.0
    assert jaro_winkler("abc", "xyz") == 0.0
    assert jaro_winkler("abc", "abd") == jaro_winkler("abd", "abc")


# --- institution matching / similarity ---


def _institutions(session: Session, *names: str) -> list[Institution]:
    rows = [Institution(name=name) for name in names]
    session.add_all(rows)
    session.commit()
    return rows


def test_find_matching_institution_uses_key_equality(session: Session) -> None:
    existing, _other = _institutions(session, "St Peters College", "Royal College")
    assert find_matching_institution(session, "St. Peter's College").id == existing.id
    assert find_matching_institution(session, "Royal Institute") is None
    assert find_matching_institution(session, "...") is None


def test_find_matching_institution_does_not_fuzzy_match(session: Session) -> None:
    _institutions(session, "University of Colombo")
    # Jaro-Winkler would score this highly, but only key equality may auto-match.
    assert find_matching_institution(session, "University of Colombo Law") is None


def test_similar_institutions_ranks_and_thresholds(session: Session) -> None:
    royal, royal_inst, _other = _institutions(
        session, "Royal College", "Royal Institute", "Zahira College"
    )
    results = similar_institutions(session, "royal college", threshold=0.9)
    assert [r.id for r in results] == [royal.id]
    assert results[0].score == 1.0

    loose = similar_institutions(session, "royal", threshold=0.99)
    # Below the score threshold, but both "Royal ..." names contain the query.
    assert {r.id for r in loose} == {royal.id, royal_inst.id}
    assert loose == sorted(loose, key=lambda r: (-r.score, r.name))
