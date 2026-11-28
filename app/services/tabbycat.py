"""Import a tournament from a Tabbycat instance into the local schema.

Tabbycat's model is richer than this app's two-team (prop/opp) schema, so records that
don't fit (BP-style debates, extra motions, unmatched score criteria, etc.) are skipped
rather than aborting the whole import; every skip is recorded on ``ImportReport.skipped``.
"""

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, NamedTuple

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from app.core.config import settings
from app.db.session import engine
from app.models import (
    Ballot,
    Debate,
    DebateJudge,
    Debater,
    Institution,
    Judge,
    Motion,
    Round,
    Side,
    SpeakerPosition,
    SpeakerScore,
    SpeakerScoreCreate,
    Team,
    TeamMember,
    Tournament,
)
from app.services.institutions import find_matching_institution, school_relation, similar_institutions
from app.services.text import institution_key, jaro_winkler, name_key, normalize_text, split_name, tidy_case

logger = logging.getLogger(__name__)

_GENDER_MAP = {"M": "male", "F": "female", "O": "other"}

# A school with no exact key match is offered for review when a fuzzy match scores at least
# this (or shares its distinguishing words). Suggestions never come from this score: see
# `_suggest_school`.
SCHOOL_SIMILAR_SCORE = 0.85
# Upstream "institutions" that stand for a mixed team rather than a school.
_COMPOSITE_SCHOOL_RE = re.compile(r"^\s*(swing|composite|mixed|independent)\b", re.IGNORECASE)
TOURNAMENT_SIMILAR_SCORE = 0.95
# A debater namesake at another school who debated within this many days may have switched
# schools, so the check leaves the choice to a human instead of suggesting "new".
DEBATER_RECENT_DAYS = 365

_CRITERION_ALIASES = {
    "content": {"content", "matter"},
    "style": {"style", "manner"},
    "strategy": {"strategy", "method"},
}


class TabbycatImportError(Exception):
    """An upstream Tabbycat request failed (non-2xx response)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TournamentAlreadyExists(Exception):
    """A local tournament with the requested slug already exists."""

    def __init__(self, slug: str) -> None:
        super().__init__(f"Tournament with slug '{slug}' already exists")
        self.slug = slug


class ImportBlocked(Exception):
    """A real (non-dry-run) import found a blocking issue and was rolled back."""

    def __init__(self, report: "ImportReport") -> None:
        blocking = [i.message for i in report.issues if i.severity == "blocking"]
        super().__init__("; ".join(blocking) or "import blocked")
        self.report = report


Severity = Literal["blocking", "decision", "warning", "info"]
SubjectKind = Literal["institution", "debater", "judge"]


class Candidate(BaseModel):
    """An existing local row an upstream school or person might be."""

    id: int
    name: str
    detail: str | None = None
    score: float | None = None


class ImportIssue(BaseModel):
    """One finding from an import or a dry run.

    `blocking` stops a real import; `decision` needs a human to pick a candidate (or "new")
    via `ImportOverrides`, keyed by `kind` + `upstream_url`; `warning` is data that will be
    skipped or looks wrong; `info` is a summary.
    """

    severity: Severity
    code: str
    message: str
    kind: SubjectKind | None = None
    upstream_url: str | None = None
    upstream_name: str | None = None
    candidates: list[Candidate] = []
    suggested: int | Literal["new"] | None = None


Choice = int | Literal["new"]


class ImportOverrides(BaseModel):
    """Human decisions for ambiguous matches: upstream URL -> local id, or "new" to create."""

    institutions: dict[str, Choice] = {}
    debaters: dict[str, Choice] = {}
    judges: dict[str, Choice] = {}
    # Used for `tournament.date` when no round has a start time upstream.
    tournament_date: date | None = None

    def for_kind(self, kind: str) -> dict[str, Choice]:
        return {"institution": self.institutions, "debater": self.debaters, "judge": self.judges}[kind]


ProgressCallback = Callable[[str, int, int], None]


class Resolution(BaseModel):
    """How one upstream institution/debater/judge was tied to a local row."""

    kind: str  # "institution" | "debater" | "judge"
    upstream_name: str
    local_id: int
    local_name: str
    how: str  # "created" | "email" | "name+institution" | "name" | "institution-key" | "override"


class ImportReport(BaseModel):
    # None only when the run stopped before creating the tournament (e.g. already imported).
    tournament_id: int | None = None
    tournament_name: str | None = None
    tournament_abbr: str | None = None
    institutions: int = 0
    teams: int = 0
    debaters: int = 0
    judges: int = 0
    rounds: int = 0
    motions: int = 0
    debates: int = 0
    ballots: int = 0
    speaker_scores: int = 0
    skipped: list[str] = []
    resolutions: list[Resolution] = []
    issues: list[ImportIssue] = []

    @property
    def blocking(self) -> list[ImportIssue]:
        return [i for i in self.issues if i.severity == "blocking"]


def describe_integrity_error(exc: IntegrityError) -> str:
    """'SQLITE_CONSTRAINT_UNIQUE: UNIQUE constraint failed: teammember.team_id, teammember.debater_id [params: (152, 531)]'"""
    orig = getattr(exc, "orig", None)
    errorname = getattr(orig, "sqlite_errorname", None)
    message = orig.args[0] if orig is not None and orig.args else str(orig or exc)
    parts = [part for part in (errorname, message) if part]
    detail = ": ".join(parts) if parts else str(exc)
    params = getattr(exc, "params", None)
    if params:
        detail = f"{detail} [params: {params}]"
    return detail


def _trunc(obj: object, limit: int = 2000) -> str:
    text = repr(obj)
    if len(text) > limit:
        return text[:limit] + "...(truncated)"
    return text


@contextmanager
def _record_context(kind: str, payload: object) -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        if not getattr(exc, "_dt_logged", False):
            exc._dt_logged = True  # sentinel: don't double-log through nested frames
            detail = describe_integrity_error(exc) if isinstance(exc, IntegrityError) else type(exc).__name__
            logger.error(
                "failed importing %s: %s\n  upstream payload: %s\n  statement: %s",
                kind,
                detail,
                _trunc(payload),
                getattr(exc, "statement", None),
                exc_info=True,
            )
        raise


def _skip(report: ImportReport, message: str, code: str = "skipped") -> None:
    """Something upstream that doesn't fit and is left out: kept in `skipped` (the legacy
    free-text list) and as a structured warning."""
    report.skipped.append(message)
    report.issues.append(ImportIssue(severity="warning", code=code, message=message))
    logger.warning(message)


def _issue(report: ImportReport, severity: Severity, code: str, message: str, **fields) -> ImportIssue:
    if severity == "decision" and not fields.get("upstream_url"):
        severity = "warning"  # nothing to key a decision on
    issue = ImportIssue(severity=severity, code=code, message=message, **fields)
    report.issues.append(issue)
    log = logger.warning if severity in ("blocking", "warning") else logger.info
    log("%s %s: %s", severity, code, message)
    return issue


class _TabbycatClient:
    """Thin wrapper over httpx.Client that normalises Tabbycat's response shapes."""

    def __init__(
        self,
        base_url: str,
        slug: str,
        api_key: str | None = None,
        *,
        http_client: httpx.Client | None = None,
        cache_dir: str | None = None,
        offline: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # Replay only: every response must already be in `cache_dir`.
        self._offline = offline
        self.slug = slug
        self._headers = {"Authorization": f"Token {api_key}"} if api_key else {}
        self._client = http_client or httpx.Client(timeout=30.0)
        self._owns_client = http_client is None
        self._cache_dir = Path(cache_dir) / slug if cache_dir else None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode()).hexdigest()
        return self._cache_dir / f"{digest}.json"

    def get(self, url: str) -> dict:
        if self._cache_dir is not None:
            cache_path = self._cache_path(url)
            if cache_path.exists():
                logger.debug("cache hit for GET %s -> %s", url, cache_path)
                return json.loads(cache_path.read_text())
        if self._offline:
            raise TabbycatImportError(
                f"staged data for {self.slug} is incomplete ({url} was never fetched); check it again"
            )

        start = time.perf_counter()
        response = self._client.get(url, headers=self._headers)
        elapsed_ms = (time.perf_counter() - start) * 1000
        if response.status_code in (401, 403):
            logger.warning("upstream GET %s failed: %s %s", url, response.status_code, response.text[:200])
            raise TabbycatImportError(
                f"API key required or invalid for {url}", status_code=response.status_code
            )
        if response.status_code == 404:
            logger.warning("upstream GET %s failed: %s %s", url, response.status_code, response.text[:200])
            raise TabbycatImportError(
                f"tournament {self.slug} not found at {self.base_url}", status_code=404
            )
        if response.status_code >= 400:
            logger.warning("upstream GET %s failed: %s %s", url, response.status_code, response.text[:200])
            raise TabbycatImportError(
                f"Tabbycat request to {url} failed with {response.status_code}: {response.text}",
                status_code=response.status_code,
            )
        logger.debug("GET %s -> %s in %.0fms (%d bytes)", url, response.status_code, elapsed_ms, len(response.content))
        data = response.json()

        if self._cache_dir is not None:
            cache_path = self._cache_path(url)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(data))
            logger.debug("cached GET %s -> %s", url, cache_path)

        return data

    def get_list(self, url: str) -> list:
        items: list = []
        next_url: str | None = url
        pages = 0
        while next_url:
            data = self.get(next_url)
            pages += 1
            if isinstance(data, list):
                items.extend(data)
                next_url = None
            else:
                items.extend(data.get("results", []))
                next_url = data.get("next")
        logger.debug("GET list %s -> %d items across %d page(s)", url, len(items), pages)
        return items


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _round_seq_from_url(round_url: str) -> int | None:
    """Tabbycat round URLs end in the round's ``seq`` (e.g. ``.../rounds/2``), which is
    the only way to associate a motion with a round: the motions endpoint links back to
    rounds by URL, not by seq directly, and the round-list endpoint doesn't embed motions
    at all (only ``motions_released``/``motions_status`` flags)."""
    try:
        return int(round_url.rstrip("/").rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return None


class _PersonResolution(NamedTuple):
    person: object
    created: bool
    how: str  # "created" | "cache-email" | "cache-name" | "db-email" | "db-name" | "name-any" | "override" | "suggested"
    key: tuple


def _resolution_how(how: str) -> str:
    if how == "created":
        return "created"
    if how.endswith("email"):
        return "email"
    return "name" if how == "name-any" else "name+institution"


def _person_label(person) -> str:
    return person.full_name or " ".join(p for p in (person.first_name, person.last_name) if p)


def _derive_names(name: str, last_name: str | None) -> tuple[str | None, str | None]:
    """First/last name for a new person: trust Tabbycat's `last_name` when present, else fall
    back to the `split_name` heuristic."""
    if last_name:
        if name.endswith(last_name):
            return (name[: -len(last_name)].strip() or None), last_name
        return (name or None), last_name
    return split_name(name)


_RELATION_ORDER = {"same": 0, "subset": 1, "different": 2}


def _rank_schools(name: str, similar: list) -> list:
    """Candidates with the same distinguishing words first, then the subset ones, then by score."""
    return sorted(similar, key=lambda c: (_RELATION_ORDER[school_relation(name, c.name)], -c.score))


def _suggest_school(name: str, similar: list) -> Choice | None:
    """The candidate to suggest, "new", or None for a human to pick.

    A wrong school match is hard to undo while a missed one is a merge, so a similarity score
    never suggests anything (Nalanda / Ananda score 0.95). A candidate is suggested only when
    its distinguishing words are the same up to spelling (`school_relation` "same"), and only if
    exactly one school is like that. When a name is the other plus extra words ("Hillwood
    College" / "Hillwood College Kandy"), the upstream may have left out the branch, so a human
    picks. When every candidate has words this one lacks and vice versa (branches: "Gateway
    College Negombo" / "Gateway College Dehiwala"), it's a new school."""
    relations = {c.id: school_relation(name, c.name) for c in similar}
    same = [c for c in similar if relations[c.id] == "same"]
    if same:
        return same[0].id if len({institution_key(c.name) for c in same}) == 1 else None
    if any(r == "subset" for r in relations.values()):
        return None
    return "new"


def _school_issue(report: ImportReport, inst: dict, similar: list, suggestion: Choice | None) -> None:
    _issue(
        report,
        "decision",
        "school_similar",
        f"School {inst['name']!r} has no exact match but looks like "
        + ", ".join(f"{c.name!r} ({round(c.score * 100)}%)" for c in similar[:3]),
        kind="institution",
        upstream_url=inst["url"],
        upstream_name=inst["name"],
        candidates=[Candidate(id=c.id, name=c.name, detail=c.code, score=c.score) for c in similar[:5]],
        suggested=suggestion,
    )


def _suggest_person(
    namesakes: list,
    institution_id: int | None,
    kind: str,
    *,
    activity: dict[int, int] | None = None,
    recent: set[int] | frozenset[int] = frozenset(),
) -> Choice | None:
    """The namesake to suggest, "new", or None when it's for a human to pick. `namesakes` are
    only the eligible ones: strong name, no conflicting email or gender, not taken this run.

    Judges judge for different schools at different tournaments, so school is ignored: a
    single namesake is the same judge, and of several (duplicates already) the one with the
    most panels in `activity` is suggested (ties: the oldest row).

    A debater is suggested when the namesake has no school or the same one (earlier imports
    often left teams without a school, so many existing debaters have none). A namesake at
    another school who debated recently (`recent`) may have switched schools, so a human
    picks; otherwise a namesake elsewhere is someone else ("new"). Several plausible
    namesakes (already duplicates here) get no suggestion."""
    if kind == "judge":
        if not namesakes:
            return "new"
        counts = activity or {}
        return max(namesakes, key=lambda n: (counts.get(n.id, 0), -n.id)).id
    home = [n for n in namesakes if n.institution_id is None or n.institution_id == institution_id]
    if any(n.id in recent for n in namesakes if n not in home):
        return None
    if not home:
        return "new"
    return home[0].id if len(home) == 1 else None


def _judge_activity(session: Session, judge_ids: list[int]) -> dict[int, int]:
    """Panels sat per judge, for choosing between existing duplicates."""
    return dict(
        session.exec(
            select(DebateJudge.judge_id, func.count())
            .where(DebateJudge.judge_id.in_(judge_ids))
            .group_by(DebateJudge.judge_id)
        ).all()
    )


def _recent_debaters(session: Session, debater_ids: list[int]) -> set[int]:
    """Debaters whose latest dated tournament is within DEBATER_RECENT_DAYS of today."""
    cutoff = date.today() - timedelta(days=DEBATER_RECENT_DAYS)
    rows = session.exec(
        select(TeamMember.debater_id, func.max(Tournament.date))
        .join(Team, Team.id == TeamMember.team_id)
        .join(Tournament, Tournament.id == Team.tournament_id)
        .where(TeamMember.debater_id.in_(debater_ids))
        .group_by(TeamMember.debater_id)
    ).all()
    return {debater_id for debater_id, latest in rows if latest is not None and latest >= cutoff}


def _strong_name(name: str) -> bool:
    """At least two words longer than an initial. A first name alone ("Akindu") or an initial
    and a surname ("K. Perera") is too common to match on: such people are only ever suggested
    as "new", with the namesakes listed for a human to link."""
    return sum(len(word) > 1 for word in re.split(r"[\s.]+", name) if word) >= 2


def _conflicts(person, email: str | None, gender: str | None) -> bool:
    """Both sides give an email, or both a gender, and they differ: not the same person by name."""
    if email and person.email and person.email.strip().lower() != email.lower():
        return True
    mapped = _GENDER_MAP.get(gender) if gender else None
    return bool(mapped and person.gender and person.gender != mapped)


def _names_compatible(a: str, b: str) -> bool:
    """For an email match: the same name, or the same surname and first initial ("Kamal
    Perera" / "K. Perera"). A shared school or coach email under someone else's name isn't."""
    left, right = name_key(a).replace(".", " ").split(), name_key(b).replace(".", " ").split()
    if not left or not right:
        return False
    return left == right or (left[-1] == right[-1] and left[0][0] == right[0][0])


def _namesake_issue(
    session: Session,
    report: ImportReport,
    model: type[Debater] | type[Judge],
    kind: str,
    name: str,
    upstream_name: str,
    source_url: str | None,
    namesakes: list,
    suggestion: Choice | None,
    preview: bool = False,
    code: str = "person_namesake",
    reason: str = "possible duplicate of",
) -> None:
    institution_names = {
        i.id: i.name
        for i in session.exec(
            select(Institution).where(Institution.id.in_({n.institution_id for n in namesakes if n.institution_id}))
        ).all()
    }
    others = ", ".join(f"#{n.id} ({institution_names.get(n.institution_id, 'none')})" for n in namesakes)
    verb = "matched by name" if isinstance(suggestion, int) and preview else "created"
    message = f"{model.__name__} {name!r} {verb}; {reason} {others}"
    report.skipped.append(message)
    _issue(
        report,
        "decision",
        code,
        message,
        kind=kind,
        upstream_url=source_url,
        upstream_name=upstream_name,
        candidates=[
            Candidate(
                id=n.id,
                name=_person_label(n),
                detail=institution_names.get(n.institution_id) if n.institution_id else None,
            )
            for n in namesakes
        ],
        suggested=suggestion,
    )


def _upsert_person(
    session: Session,
    model: type[Debater] | type[Judge],
    cache: dict[tuple, object],
    report: ImportReport,
    *,
    name: str,
    last_name: str | None,
    email: str | None,
    phone: str | None,
    gender: str | None,
    institution_id: int | None,
    source_url: str | None = None,
    overrides: dict[str, Choice] | None = None,
    created_ids: set[int] | None = None,
    claimed: dict[int, str | None] | None = None,
    preview: bool = False,
) -> _PersonResolution:
    """Resolve one upstream speaker/adjudicator to a local row.

    A wrong match is hard to undo (no un-merge) while a missed one is a merge, so only strong
    evidence matches silently:

    1. A human override for `source_url` (an id).
    2. The per-run cache. For a debater it's keyed by email, else name + school, so a speaker
       listed on two teams of one school (reserves) is one debater. For a judge it's keyed by
       the upstream URL: each adjudicator is listed once, so two of one name are two people.
    3. Email, when the names are compatible (`_names_compatible`). An email held by someone
       with another name is an `email_other_name` decision, suggested "new".
    4. A strong name (`_strong_name`) at the same school, with no conflicting email / gender.
    5. A judge with a strong name and exactly one namesake anywhere (judges represent different
       schools at different tournaments), again with no conflict.

    Otherwise a new row is created, and any namesakes become a `person_namesake` decision.
    Weak names and conflicting namesakes are never suggested. For judges, `claimed` maps each
    local row this run resolved to the upstream URL it went to: a second adjudicator never takes
    a claimed row (two judges of one name in one tournament stay apart, `same_name_twice`).
    `created_ids` holds the rows this run created, never offered as candidates (a dry run rolls
    them back). With `preview` (a dry run) a suggested namesake is used instead of creating
    someone, so the report shows what the import does with the suggestions accepted. An
    override of "new" rules out the namesakes but not steps 3-4.
    """
    upstream_name = (name or "").strip()
    name = tidy_case(normalize_text(name))
    last_name = normalize_text(last_name) or None
    email = email.strip() if email else None
    kind = model.__name__.lower()
    nkey = name_key(name)
    strong = _strong_name(name)
    if kind == "judge" and source_url:
        key: tuple = ("url", source_url)
    elif email:
        key = ("email", email.lower())
    else:
        key = ("name", nkey, institution_id)
    created_ids = created_ids if created_ids is not None else set()
    claimed = claimed if claimed is not None else {}
    choice = (overrides or {}).get(source_url) if source_url else None

    def free(person) -> bool:
        return kind != "judge" or claimed.get(person.id, source_url) == source_url

    def resolved(person, created: bool, how: str) -> _PersonResolution:
        cache[key] = person
        claimed.setdefault(person.id, source_url)
        report.resolutions.append(
            Resolution(
                kind=kind,
                upstream_name=upstream_name,
                local_id=person.id,
                local_name=_person_label(person),
                how=how if how in ("override", "suggested") else _resolution_how(how),
            )
        )
        return _PersonResolution(person, created, how, key)

    if isinstance(choice, int):
        chosen = session.get(model, choice)
        if chosen is not None:
            logger.debug("%s %r resolved via override to id=%s (upstream %s)", model.__name__, name, choice, source_url)
            return resolved(chosen, False, "override")
        _issue(
            report,
            "blocking",
            "override_missing",
            f"{model.__name__} {upstream_name!r} was matched to #{choice}, which no longer exists; check again",
            kind=kind,
            upstream_url=source_url,
            upstream_name=upstream_name,
        )

    cached = cache.get(key)
    if cached is not None:
        logger.debug("%s %r resolved via cache key=%r", model.__name__, name, key)
        return resolved(cached, False, "cache-email" if key[0] == "email" else "cache-name")

    kept_apart: list = []  # matches already taken by another upstream record this run
    email_holder = None
    if email:
        holder = session.exec(select(model).where(func.lower(model.email) == email.lower()).order_by(model.id)).first()
        if holder is not None and not free(holder):
            kept_apart.append(holder)
        elif holder is not None and _names_compatible(_person_label(holder), name):
            logger.debug("%s %r resolved via email to id=%s (upstream %s)", model.__name__, name, holder.id, source_url)
            return resolved(holder, False, "db-email")
        elif holder is not None and choice != "new":
            email_holder = holder

    if strong:
        for existing in session.exec(
            select(model)
            .where(func.lower(model.full_name) == nkey, model.institution_id == institution_id)
            .order_by(model.id)
        ).all():
            if not free(existing):
                kept_apart.append(existing)
            elif not _conflicts(existing, email, gender):
                logger.debug(
                    "%s %r resolved via name+school to id=%s (upstream %s)", model.__name__, name, existing.id, source_url
                )
                return resolved(existing, False, "db-name")

    namesakes = (
        []
        if choice == "new"
        else [
            n
            for n in session.exec(select(model).where(func.lower(model.full_name) == nkey).order_by(model.id)).all()
            if n.id not in created_ids
        ]
    )
    eligible = [n for n in namesakes if free(n) and not _conflicts(n, email, gender)] if strong else []

    if kind == "judge" and len(namesakes) == 1 and eligible == namesakes:
        logger.debug("Judge %r matched namesake id=%s at another school (upstream %s)", name, eligible[0].id, source_url)
        return resolved(eligible[0], False, "name-any")

    ids = [n.id for n in eligible]
    if not namesakes:
        suggestion: Choice | None = "new"
    elif kind == "judge":
        suggestion = _suggest_person(eligible, institution_id, kind, activity=_judge_activity(session, ids) if ids else {})
    else:
        recent = _recent_debaters(session, ids) if ids else set()
        suggestion = _suggest_person(eligible, institution_id, kind, recent=recent)
    if preview and isinstance(suggestion, int):
        person = next(n for n in namesakes if n.id == suggestion)
        _namesake_issue(session, report, model, kind, name, upstream_name, source_url, namesakes, suggestion, preview)
        return resolved(person, False, "suggested")

    first_name, resolved_last_name = _derive_names(name, last_name)
    person = model(
        full_name=name,
        first_name=first_name,
        last_name=resolved_last_name,
        email=email,
        phone=phone or None,
        gender=_GENDER_MAP.get(gender) if gender else None,
        institution_id=institution_id,
    )
    session.add(person)
    session.flush()
    created_ids.add(person.id)
    logger.debug("%s %r created id=%s key=%r (upstream %s)", model.__name__, name, person.id, key, source_url)

    if kept_apart:
        _issue(
            report,
            "warning",
            "same_name_twice",
            f"Two adjudicators named {name!r} in this tournament were kept apart; merge them if they are one person",
            kind=kind,
            upstream_url=source_url,
            upstream_name=upstream_name,
        )
    if namesakes:
        if email_holder is not None and all(n.id != email_holder.id for n in namesakes):
            namesakes = [*namesakes, email_holder]
        _namesake_issue(session, report, model, kind, name, upstream_name, source_url, namesakes, suggestion)
    elif email_holder is not None:
        _namesake_issue(
            session,
            report,
            model,
            kind,
            name,
            upstream_name,
            source_url,
            [email_holder],
            "new",
            code="email_other_name",
            reason="another person has the same email:",
        )
    return resolved(person, True, "created")


def _normalize_side(value) -> str | None:
    if value == "aff" or value == 0:
        return "prop"
    if value == "neg" or value == 1:
        return "opp"
    return None


def _match_criteria(
    criteria: list[dict], criterion_name_by_url: dict[str, str]
) -> tuple[float, float, float] | None:
    scores: dict[str, float] = {}
    for entry in criteria:
        name = criterion_name_by_url.get(entry.get("criterion"))
        if not name:
            continue
        lowered = name.strip().lower()
        for category, aliases in _CRITERION_ALIASES.items():
            if category not in scores and lowered in aliases:
                scores[category] = entry.get("score")
    if all(category in scores for category in _CRITERION_ALIASES):
        return scores["content"], scores["style"], scores["strategy"]
    return None


def _import_ballots(
    session: Session,
    client: _TabbycatClient,
    round_seq: int,
    debate_pk: int,
    debate: Debate,
    *,
    judge_by_url: dict[str, int],
    team_by_url: dict[str, int],
    debater_by_url: dict[str, int],
    criterion_name_by_url: dict[str, str],
    chair_judge_id: int | None,
    judge_ids_on_panel: set[int],
    report: ImportReport,
) -> None:
    with _record_context(
        f"ballots for debate {debate.id}", {"round_seq": round_seq, "debate_pk": debate_pk}
    ):
        ballots_url = client.url(
            f"/api/v1/tournaments/{client.slug}/rounds/{round_seq}/pairings/{debate_pk}/ballots?confirmed=true"
        )
        submissions = client.get_list(ballots_url)
        logger.debug("debate %s: %d ballot submission(s) fetched", debate.id, len(submissions))

        sheet_by_judge: dict[int, tuple[int, dict, dict]] = {}
        for submission in submissions:
            if submission.get("discarded"):
                continue
            version = submission.get("version") or 0
            for sheet in (submission.get("result") or {}).get("sheets", []):
                adjudicator_url = sheet.get("adjudicator")
                if adjudicator_url:
                    judge_id = judge_by_url.get(adjudicator_url)
                    if judge_id is None:
                        _skip(
                            report,
                            f"Debate {debate.id}: ballot adjudicator not in imported panel, skipped",
                            "ballot_judge_unknown",
                        )
                        continue
                else:
                    judge_id = chair_judge_id
                    if judge_id is None:
                        _skip(
                            report,
                            f"Debate {debate.id}: consensus ballot has no chair to attribute, skipped",
                            "ballot_no_chair",
                        )
                        continue
                if judge_id not in judge_ids_on_panel:
                    _skip(
                        report,
                        f"Debate {debate.id}: ballot judge {judge_id} not on imported panel, skipped",
                        "ballot_judge_unknown",
                    )
                    continue

                existing = sheet_by_judge.get(judge_id)
                if existing is not None:
                    _skip(
                        report,
                        f"Debate {debate.id}: duplicate ballot for judge {judge_id}, kept highest version",
                        "duplicate_ballot",
                    )
                    if version <= existing[0]:
                        continue
                sheet_by_judge[judge_id] = (version, sheet, submission)

        any_ballot = False
        debate_ballots = 0
        debate_scores = 0
        for judge_id, (_version, sheet, submission) in sheet_by_judge.items():
            teams = sheet.get("teams") or []
            winner_side = None
            for team_result in teams:
                if team_result.get("win"):
                    team_id = team_by_url.get(team_result.get("team"))
                    if team_id == debate.prop_team_id:
                        winner_side = Side.PROP
                    elif team_id == debate.opp_team_id:
                        winner_side = Side.OPP
            if winner_side is None:
                _skip(
                    report,
                    f"Debate {debate.id}: ballot for judge {judge_id} has no confirmed winner, skipped",
                    "ballot_no_winner",
                )
                continue

            ballot = Ballot(
                debate_id=debate.id,
                judge_id=judge_id,
                winner=winner_side,
                forfeit=bool(submission.get("forfeit", False)),
            )
            session.add(ballot)
            session.flush()
            report.ballots += 1
            debate_ballots += 1
            any_ballot = True

            for team_result in teams:
                team_id = team_by_url.get(team_result.get("team"))
                if team_id == debate.prop_team_id:
                    side = Side.PROP
                elif team_id == debate.opp_team_id:
                    side = Side.OPP
                else:
                    continue

                speeches = team_result.get("speeches") or []
                if len(speeches) > 4:
                    _skip(
                        report,
                        f"Debate {debate.id} judge {judge_id}: {len(speeches)} speeches, kept first 4",
                        "extra_speeches",
                    )
                for index, speech in enumerate(speeches[:4]):
                    debater_id = debater_by_url.get(speech.get("speaker"))
                    if debater_id is None:
                        _skip(
                            report,
                            f"Debate {debate.id} judge {judge_id}: speech speaker not found, skipped",
                            "unknown_speaker",
                        )
                        continue

                    breakdown = _match_criteria(speech.get("criteria") or [], criterion_name_by_url)
                    content, style, strategy = breakdown if breakdown else (None, None, None)
                    try:
                        score_in = SpeakerScoreCreate(
                            debater_id=debater_id,
                            side=side,
                            position=SpeakerPosition(index + 1),
                            content=content,
                            style=style,
                            strategy=strategy,
                            final_score=speech.get("score") if breakdown is None else None,
                        )
                    except ValidationError as exc:
                        _skip(
                            report,
                            f"Debate {debate.id} judge {judge_id}: invalid speaker score, skipped ({exc})",
                            "invalid_score",
                        )
                        continue

                    row = SpeakerScore.model_validate(score_in, update={"ballot_id": ballot.id})
                    session.add(row)
                    report.speaker_scores += 1
                    debate_scores += 1
            session.flush()

        if any_ballot:
            _recompute_debate_winner(session, debate)

        logger.info("debate %s: %d ballot(s), %d score(s)", debate.id, debate_ballots, debate_scores)


def _recompute_debate_winner(session: Session, debate: Debate) -> None:
    """Majority-vote winner. Mirrors `_recompute_debate_winner` in
    app/api/v1/endpoints/ballots.py (kept separate since services shouldn't import
    from the API layer)."""
    ballots = session.exec(select(Ballot).where(Ballot.debate_id == debate.id)).all()
    counted = [b for b in ballots if not b.discarded]
    prop_votes = sum(1 for b in counted if b.winner == Side.PROP)
    opp_votes = sum(1 for b in counted if b.winner == Side.OPP)
    if prop_votes > opp_votes:
        debate.winner = Side.PROP
        session.add(debate)
    elif opp_votes > prop_votes:
        debate.winner = Side.OPP
        session.add(debate)


def _run_import(
    session: Session,
    client: _TabbycatClient,
    base_url: str,
    slug: str,
    include_ballots: bool,
    *,
    report_sink: list[ImportReport] | None = None,
    overrides: ImportOverrides | None = None,
    progress: ProgressCallback | None = None,
    preview: bool = False,
) -> ImportReport:
    logger.info(
        "import start slug=%s base_url=%s include_ballots=%s cache_dir=%s",
        slug,
        base_url,
        include_ballots,
        client._cache_dir or "(disabled)",
    )
    existing = session.exec(select(Tournament).where(Tournament.slug == slug)).first()
    if existing is not None:
        raise TournamentAlreadyExists(slug)

    overrides = overrides or ImportOverrides()

    def step(phase: str, done: int, total: int) -> None:
        if progress is not None:
            progress(phase, done, total)

    step("Fetching tournament", 0, 0)
    tourney_data = client.get(client.url(f"/api/v1/tournaments/{slug}"))
    similar_tournaments = [
        t
        for t in session.exec(select(Tournament)).all()
        if jaro_winkler(name_key(t.name), name_key(tourney_data.get("name") or "")) >= TOURNAMENT_SIMILAR_SCORE
    ]
    with _record_context(f"tournament {slug}", tourney_data):
        tournament = Tournament(
            name=tourney_data["name"],
            abbr=tourney_data.get("short_name") or None,
            base_url=base_url,
            slug=slug,
        )
        session.add(tournament)
        session.flush()
    report = ImportReport(
        tournament_id=tournament.id, tournament_name=tournament.name, tournament_abbr=tournament.abbr
    )
    if report_sink is not None:
        report_sink.append(report)
    for other in similar_tournaments:
        _issue(
            report,
            "warning",
            "tournament_name_similar",
            f"An existing tournament has a very similar name: {other.name!r} (#{other.id}, slug {other.slug!r}). "
            f"Make sure this isn't the same tournament under another slug.",
        )
    created_ids: dict[str, set[int]] = {"institution": set(), "debater": set(), "judge": set()}
    claimed: dict[str, dict[int, str | None]] = {"debater": {}, "judge": {}}
    logger.info("tournament local_id=%s name=%r abbr=%r", tournament.id, tournament.name, tournament.abbr)

    institution_by_url: dict[str, int] = {}
    composite_urls: set[str] = set()
    institutions_fetched = 0
    institutions_matched = 0
    step("Fetching schools", 0, 0)
    for inst in client.get_list(client.url("/api/v1/institutions")):
        institutions_fetched += 1
        with _record_context(f"institution {inst.get('name')!r}", inst):
            if _COMPOSITE_SCHOOL_RE.match(inst.get("name") or ""):
                composite_urls.add(inst["url"])
                logger.debug("institution %r is a swing/composite team, not a school", inst["name"])
                continue
            choice = overrides.institutions.get(inst["url"])
            if isinstance(choice, int):
                chosen = session.get(Institution, choice)
                if chosen is not None:
                    institution_by_url[inst["url"]] = chosen.id
                    report.resolutions.append(
                        Resolution(
                            kind="institution",
                            upstream_name=inst["name"],
                            local_id=chosen.id,
                            local_name=chosen.name,
                            how="override",
                        )
                    )
                    continue
                _issue(
                    report,
                    "blocking",
                    "override_missing",
                    f"School {inst['name']!r} was matched to #{choice}, which no longer exists; check again",
                    kind="institution",
                    upstream_url=inst["url"],
                    upstream_name=inst["name"],
                )
            # "new" only rules out the fuzzy candidates; an exact key match (say a school an
            # earlier tournament in the same batch created) still applies.
            existing_inst = find_matching_institution(session, inst["name"])
            if existing_inst is not None:
                institution_by_url[inst["url"]] = existing_inst.id
                institutions_matched += 1
                report.resolutions.append(
                    Resolution(
                        kind="institution",
                        upstream_name=inst["name"],
                        local_id=existing_inst.id,
                        local_name=existing_inst.name,
                        how="institution-key",
                    )
                )
                logger.debug("institution %r matched existing id=%s", inst["name"], existing_inst.id)
                continue
            similar = (
                []
                if choice is not None
                else _rank_schools(
                    inst["name"],
                    [
                        c
                        for c in similar_institutions(session, inst["name"], threshold=SCHOOL_SIMILAR_SCORE)
                        if c.id not in created_ids["institution"]
                    ],
                )
            )
            suggestion = _suggest_school(inst["name"], similar)
            if similar and preview and isinstance(suggestion, int):
                chosen = session.get(Institution, suggestion)
                institution_by_url[inst["url"]] = chosen.id
                _school_issue(report, inst, similar, suggestion)
                report.resolutions.append(
                    Resolution(
                        kind="institution",
                        upstream_name=inst["name"],
                        local_id=chosen.id,
                        local_name=chosen.name,
                        how="suggested",
                    )
                )
                continue
            new_inst = Institution(name=normalize_text(inst["name"]), code=inst.get("code") or None)
            session.add(new_inst)
            session.flush()
            created_ids["institution"].add(new_inst.id)
            if similar:
                _school_issue(report, inst, similar, suggestion)
            institution_by_url[inst["url"]] = new_inst.id
            report.institutions += 1
            report.resolutions.append(
                Resolution(
                    kind="institution",
                    upstream_name=inst["name"],
                    local_id=new_inst.id,
                    local_name=new_inst.name,
                    how="created",
                )
            )
            logger.debug("institution %r created id=%s code=%r", inst["name"], new_inst.id, inst.get("code"))
    if composite_urls:
        _issue(
            report,
            "info",
            "composite_school",
            f"{len(composite_urls)} swing/composite team listing(s) treated as no school, so their "
            f"teams and new speakers get no school",
        )
    logger.info(
        "institutions: %d fetched, %d created, %d matched by institution key",
        institutions_fetched,
        report.institutions,
        institutions_matched,
    )

    team_by_url: dict[str, int] = {}
    debater_by_url: dict[str, int] = {}
    debater_cache: dict[tuple, object] = {}
    teams_fetched = 0
    teams_no_institution_url = 0
    teams_institution_unresolved = 0
    debaters_reused = 0
    step("Fetching teams", 0, 0)
    teams_payload = client.get_list(client.url(f"/api/v1/tournaments/{slug}/teams"))
    for team_data in teams_payload:
        teams_fetched += 1
        step("Importing teams", teams_fetched, len(teams_payload))
        name = team_data.get("long_name") or team_data.get("short_name") or team_data.get("reference")
        with _record_context(f"team {name!r} (upstream {team_data.get('url')})", team_data):
            institution_url = team_data.get("institution")
            if institution_url in composite_urls:
                institution_id = None
            elif institution_url is None:
                institution_id = None
                teams_no_institution_url += 1
            else:
                institution_id = institution_by_url.get(institution_url)
                if institution_id is None:
                    teams_institution_unresolved += 1
                    _skip(
                        report,
                        f"Team {name!r}: institution {institution_url} did not resolve, institution_id left NULL",
                        "unresolved_institution",
                    )

            team = Team(tournament_id=tournament.id, name=name, institution_id=institution_id)
            session.add(team)
            session.flush()
            team_by_url[team_data["url"]] = team.id
            report.teams += 1
            logger.debug("team %r created id=%s institution_id=%s", name, team.id, institution_id)

            seen_debaters: dict[int, str] = {}
            speakers = team_data.get("speakers", [])
            # Teams often register reserves, so only a team that can't field three is unusual.
            if len(speakers) < 3:
                _issue(
                    report,
                    "warning",
                    "team_size",
                    f"Team {name!r} has only {len(speakers)} speaker(s)",
                )
            for speaker in speakers:
                res = _upsert_person(
                    session,
                    Debater,
                    debater_cache,
                    report,
                    name=speaker.get("name", ""),
                    last_name=speaker.get("last_name"),
                    email=speaker.get("email"),
                    phone=speaker.get("phone"),
                    gender=speaker.get("gender"),
                    institution_id=institution_id,
                    source_url=speaker.get("url"),
                    overrides=overrides.debaters,
                    created_ids=created_ids["debater"],
                    claimed=claimed["debater"],
                    preview=preview,
                )
                debater = res.person
                if res.created:
                    report.debaters += 1
                else:
                    debaters_reused += 1
                debater_by_url[speaker["url"]] = debater.id

                if debater.id in seen_debaters:
                    logger.warning(
                        "team %r: speaker %s (%r) resolved to debater id=%s already on this team via %s "
                        "[resolution=%s key=%r] — duplicate TeamMember skipped (would violate uniq(team_id, debater_id))",
                        name,
                        speaker["url"],
                        speaker.get("name"),
                        debater.id,
                        seen_debaters[debater.id],
                        res.how,
                        res.key,
                    )
                    _skip(
                        report,
                        f"Team {name!r}: speaker {speaker.get('name')!r} resolved to a debater already "
                        f"on this team, duplicate TeamMember skipped",
                        "duplicate_member",
                    )
                    continue
                seen_debaters[debater.id] = speaker["url"]
                session.add(TeamMember(team_id=team.id, debater_id=debater.id))
            session.flush()

    if teams_no_institution_url:
        _skip(
            report,
            f"{teams_no_institution_url} of {teams_fetched} teams had no institution in the upstream "
            f"payload (institution_id left NULL)",
            "team_no_institution",
        )
    logger.info(
        "teams: %d created, %d debaters created, %d reused, %d teams with unresolved institution",
        report.teams,
        report.debaters,
        debaters_reused,
        teams_institution_unresolved,
    )

    judge_by_url: dict[str, int] = {}
    judge_cache: dict[tuple, object] = {}
    adjs_fetched = 0
    adjs_no_institution = 0
    judges_reused = 0
    judges_other_school: list[str] = []
    step("Fetching adjudicators", 0, 0)
    adjs_payload = client.get_list(client.url(f"/api/v1/tournaments/{slug}/adjudicators"))
    for adj in adjs_payload:
        adjs_fetched += 1
        step("Importing adjudicators", adjs_fetched, len(adjs_payload))
        with _record_context(f"adjudicator {adj.get('name')!r}", adj):
            institution_url = adj.get("institution")
            if institution_url in composite_urls:
                institution_id = None
            elif institution_url is None:
                institution_id = None
                adjs_no_institution += 1
            else:
                institution_id = institution_by_url.get(institution_url)
                if institution_id is None:
                    _skip(
                        report,
                        f"Adjudicator {adj.get('name')!r}: institution {institution_url} did not resolve, "
                        f"institution_id left NULL",
                        "unresolved_institution",
                    )
            res = _upsert_person(
                session,
                Judge,
                judge_cache,
                report,
                name=adj.get("name", ""),
                last_name=adj.get("last_name"),
                email=adj.get("email"),
                phone=adj.get("phone"),
                gender=adj.get("gender"),
                institution_id=institution_id,
                source_url=adj.get("url"),
                overrides=overrides.judges,
                created_ids=created_ids["judge"],
                claimed=claimed["judge"],
                preview=preview,
            )
            judge = res.person
            if res.created:
                report.judges += 1
            else:
                judges_reused += 1
                if res.how == "name-any":
                    judges_other_school.append(_person_label(judge))
            judge_by_url[adj["url"]] = judge.id
            logger.debug(
                "adjudicator %r resolved to judge id=%s institution_id=%s", adj.get("name"), judge.id, institution_id
            )

    if adjs_no_institution:
        _skip(
            report,
            f"{adjs_no_institution} of {adjs_fetched} adjudicators had no institution in the upstream "
            f"payload (institution_id left NULL)",
            "judge_no_institution",
        )
    if judges_other_school:
        _issue(
            report,
            "info",
            "judge_other_school",
            f"{len(judges_other_school)} judge(s) matched an existing judge of the same name listed "
            f"under another school: " + ", ".join(judges_other_school),
        )
    logger.info(
        "adjudicators: %d created, %d reused, %d with no institution",
        report.judges,
        judges_reused,
        adjs_no_institution,
    )

    step("Fetching rounds and motions", 0, 0)
    venue_name_by_url: dict[str, str] = {
        venue["url"]: venue.get("name") or venue.get("display_name")
        for venue in client.get_list(client.url(f"/api/v1/tournaments/{slug}/venues"))
    }

    criterion_name_by_url: dict[str, str] = {
        criterion["url"]: criterion.get("name", "")
        for criterion in client.get_list(client.url(f"/api/v1/tournaments/{slug}/score-criteria"))
    }
    logger.debug(
        "venues: %d, score-criteria: %d (%s)",
        len(venue_name_by_url),
        len(criterion_name_by_url),
        ", ".join(criterion_name_by_url.values()),
    )

    motions_by_round_seq: dict[int, list[dict]] = {}
    for motion in client.get_list(client.url(f"/api/v1/tournaments/{slug}/motions")):
        for round_ref in motion.get("rounds") or []:
            seq = _round_seq_from_url(round_ref.get("round", ""))
            if seq is not None:
                motions_by_round_seq.setdefault(seq, []).append(motion)
    logger.debug(
        "motions: %d fetched from upstream, assigned to %d round(s)",
        sum(len(v) for v in motions_by_round_seq.values()),
        len(motions_by_round_seq),
    )

    starts_ats: list[date] = []
    round_ids_by_seq: dict[int, int] = {}
    rounds_fetched = 0
    rounds_no_motion = 0
    for round_data in client.get_list(client.url(f"/api/v1/tournaments/{slug}/rounds")):
        rounds_fetched += 1
        with _record_context(f"round seq={round_data.get('seq')}", round_data):
            round_ = Round(
                tournament_id=tournament.id,
                seq=round_data["seq"],
                abbr=round_data.get("abbreviation") or None,
                name=round_data.get("name") or None,
                isElimination=round_data.get("stage") == "E",
                completed=bool(round_data.get("completed", False)),
            )
            session.add(round_)
            session.flush()
            report.rounds += 1
            round_ids_by_seq[round_data["seq"]] = round_.id
            logger.debug("round seq=%s created id=%s", round_data["seq"], round_.id)

            starts_at = _parse_datetime(round_data.get("starts_at"))
            if starts_at is not None:
                starts_ats.append(starts_at.date())

            motions = motions_by_round_seq.get(round_data["seq"], [])
            if motions:
                first_motion = motions[0]
                session.add(
                    Motion(
                        round_id=round_.id,
                        text=normalize_text(first_motion["text"]),
                        info_slide=normalize_text(first_motion.get("info_slide")) or None,
                    )
                )
                session.flush()
                report.motions += 1
                if len(motions) > 1:
                    _skip(
                        report,
                        f"Round {round_data['seq']}: kept first of {len(motions)} motions, "
                        f"{len(motions) - 1} extra motion(s) skipped",
                        "extra_motion",
                    )
            else:
                rounds_no_motion += 1

    if rounds_no_motion:
        _skip(
            report,
            f"{rounds_no_motion} of {rounds_fetched} rounds had no motion in the upstream payload "
            f"(0 motions imported for them)",
            "missing_motion",
        )
        if rounds_no_motion == rounds_fetched:
            logger.warning("no motions at all in %d round(s) — total motion data loss", rounds_no_motion)
    logger.info(
        "rounds: %d created, %d motions imported, %d rounds with no motion in payload",
        report.rounds,
        report.motions,
        rounds_no_motion,
    )

    pairings_seen = 0
    debates_without_panel = 0
    rounds_without_results: list[int] = []
    total_rounds = len(round_ids_by_seq)
    for round_index, (seq, round_id) in enumerate(round_ids_by_seq.items(), start=1):
        step(f"Round {seq}: fetching pairings", round_index - 1, total_rounds)
        pairings = client.get_list(client.url(f"/api/v1/tournaments/{slug}/rounds/{seq}/pairings"))
        pairings_seen += len(pairings)
        round_debates: list[Debate] = []
        logger.info("round %d: %d pairings fetched", seq, len(pairings))
        round_debates_created = 0
        round_pairings_skipped = 0
        for pairing_index, pairing in enumerate(pairings, start=1):
            if include_ballots:
                step(f"Round {seq}: fetching ballots", pairing_index - 1, len(pairings))
            with _record_context(f"round {seq} pairing {pairing.get('id')}", pairing):
                teams = pairing.get("teams") or []
                if len(teams) != 2:
                    _skip(
                        report,
                        f"Round {seq} pairing {pairing.get('id')}: expected 2 teams, got {len(teams)}, skipped",
                        "bp_debate" if len(teams) == 4 else "bye",
                    )
                    round_pairings_skipped += 1
                    continue

                sides: dict[str, str] = {}
                for team_entry in teams:
                    side = _normalize_side(team_entry.get("side"))
                    if side is None or side in sides:
                        sides = {}
                        break
                    sides[side] = team_entry.get("team")
                if "prop" not in sides or "opp" not in sides:
                    _skip(
                        report,
                        f"Round {seq} pairing {pairing.get('id')}: unmappable sides (BP or bye), skipped",
                        "unmappable_sides",
                    )
                    round_pairings_skipped += 1
                    continue

                prop_team_id = team_by_url.get(sides["prop"])
                opp_team_id = team_by_url.get(sides["opp"])
                if prop_team_id is None or opp_team_id is None:
                    _skip(
                        report,
                        f"Round {seq} pairing {pairing.get('id')}: team not found in imported set, skipped",
                        "unknown_team",
                    )
                    round_pairings_skipped += 1
                    continue

                venue_url = pairing.get("venue")
                room = venue_name_by_url.get(venue_url) if venue_url else None

                debate = Debate(
                    round_id=round_id, prop_team_id=prop_team_id, opp_team_id=opp_team_id, room=room
                )
                session.add(debate)
                session.flush()
                report.debates += 1
                round_debates_created += 1
                round_debates.append(debate)
                logger.debug(
                    "round %d debate id=%s: prop=%s opp=%s room=%r", seq, debate.id, prop_team_id, opp_team_id, room
                )

                judge_ids_on_panel: set[int] = set()
                chair_judge_id: int | None = None
                adjudicators = pairing.get("adjudicators")
                if adjudicators:
                    chair_url = adjudicators.get("chair")
                    if chair_url:
                        chair_judge_id = judge_by_url.get(chair_url)
                        if chair_judge_id is not None:
                            session.add(DebateJudge(debate_id=debate.id, judge_id=chair_judge_id, is_chair=True))
                            judge_ids_on_panel.add(chair_judge_id)
                    for panellist_url in adjudicators.get("panellists") or []:
                        judge_id = judge_by_url.get(panellist_url)
                        if judge_id is not None:
                            if judge_id in judge_ids_on_panel:
                                logger.warning(
                                    "debate %s: judge id=%s listed as panellist but already on panel "
                                    "(e.g. chair) — would violate uniq(debate_id, judge_id), skipping duplicate",
                                    debate.id,
                                    judge_id,
                                )
                                continue
                            session.add(DebateJudge(debate_id=debate.id, judge_id=judge_id))
                            judge_ids_on_panel.add(judge_id)
                    for trainee_url in adjudicators.get("trainees") or []:
                        judge_id = judge_by_url.get(trainee_url)
                        if judge_id is not None:
                            if judge_id in judge_ids_on_panel:
                                logger.warning(
                                    "debate %s: judge id=%s listed as trainee but already on panel "
                                    "— would violate uniq(debate_id, judge_id), skipping duplicate",
                                    debate.id,
                                    judge_id,
                                )
                                continue
                            session.add(DebateJudge(debate_id=debate.id, judge_id=judge_id, is_trainee=True))
                            judge_ids_on_panel.add(judge_id)
                    session.flush()
                if not judge_ids_on_panel:
                    debates_without_panel += 1

                if include_ballots:
                    _import_ballots(
                        session,
                        client,
                        seq,
                        pairing["id"],
                        debate,
                        judge_by_url=judge_by_url,
                        team_by_url=team_by_url,
                        debater_by_url=debater_by_url,
                        criterion_name_by_url=criterion_name_by_url,
                        chair_judge_id=chair_judge_id,
                        judge_ids_on_panel=judge_ids_on_panel,
                        report=report,
                    )
        if include_ballots and round_debates and all(d.winner is None for d in round_debates):
            rounds_without_results.append(seq)
        logger.info(
            "round %d: %d debate(s) created, %d pairing(s) skipped", seq, round_debates_created, round_pairings_skipped
        )

    if debates_without_panel:
        _issue(
            report,
            "warning",
            "no_adjudicator",
            f"{debates_without_panel} debate(s) have no adjudicator on the imported panel",
        )
    if rounds_without_results:
        _issue(
            report,
            "warning",
            "round_without_results",
            "No confirmed results in round(s) " + ", ".join(str(r) for r in rounds_without_results)
            + "; their debates will be imported without winners",
        )
    if pairings_seen and report.debates == 0:
        _issue(
            report,
            "blocking",
            "no_two_team_debates",
            f"None of the {pairings_seen} pairings is a two-team debate (BP or byes only), so nothing would be imported",
        )

    if starts_ats:
        tournament.date = min(starts_ats)
        session.add(tournament)
        logger.info("tournament.date set to %s from %d round start time(s)", tournament.date, len(starts_ats))
    elif overrides.tournament_date is not None:
        tournament.date = overrides.tournament_date
        session.add(tournament)
        _issue(report, "info", "date_by_hand", f"No round had a start time; date set by hand to {tournament.date}")
    else:
        _skip(report, "no round had starts_at; tournament.date left NULL", "missing_start")

    matched = {kind: 0 for kind in ("institution", "debater", "judge")}
    for resolution in report.resolutions:
        if resolution.how != "created":
            matched[resolution.kind] += 1
    _issue(
        report,
        "info",
        "matched_existing",
        f"{matched['institution']} school(s), {matched['debater']} debater record(s) and "
        f"{matched['judge']} judge record(s) matched existing records",
    )

    logger.info("import complete slug=%s %s", slug, report.model_dump(exclude={"skipped", "issues", "resolutions"}))
    return report


def import_tournament(
    base_url: str,
    slug: str,
    api_key: str | None = None,
    *,
    session: Session | None = None,
    include_ballots: bool = True,
    dry_run: bool = False,
    overrides: ImportOverrides | None = None,
    progress: ProgressCallback | None = None,
    cache_dir: str | None = None,
    offline: bool = False,
) -> ImportReport:
    """Fetch a tournament from a Tabbycat instance and populate the local DB.

    Raises `TournamentAlreadyExists` if a local tournament with `slug` already exists,
    and `TabbycatImportError` on upstream HTTP failures. `httpx` transport-level errors
    (timeouts, connection failures) propagate unchanged. With `include_ballots=True`
    this makes `1 + rounds + debates` upstream requests and can take a while for a
    large tournament.

    With `dry_run=True` the whole import runs, then rolls back instead of committing, so the
    returned report (counts, skips, resolutions) shows what a real import would do. The
    report's `tournament_id` refers to a row that no longer exists.

    `overrides` applies human decisions for ambiguous schools/people (see `ImportIssue`).
    A real import with any `blocking` issue is rolled back and raises `ImportBlocked`.
    `cache_dir` overrides `settings.tabbycat_cache_dir`; with `offline=True` every upstream
    response must already be cached there (replaying a checked import without the API key).
    """
    api_key = api_key or settings.tabbycat_api_key
    owns_session = session is None
    active_session = session or Session(engine)
    client = _TabbycatClient(
        base_url,
        slug,
        api_key,
        cache_dir=cache_dir or settings.tabbycat_cache_dir,
        offline=offline,
    )
    sink: list[ImportReport] = []
    try:
        report = _run_import(
            active_session,
            client,
            base_url,
            slug,
            include_ballots,
            report_sink=sink,
            overrides=overrides,
            progress=progress,
            preview=dry_run,
        )
        if dry_run:
            active_session.rollback()
            logger.info("import preview slug=%s rolled back (dry run)", slug)
        elif report.blocking:
            active_session.rollback()
            raise ImportBlocked(report)
        else:
            active_session.commit()
        return report
    except Exception as exc:
        partial = sink[0] if sink else None
        logger.error("import FAILED slug=%s: %s", slug, exc)
        if partial is not None:
            logger.error("  partial progress before rollback: %s", partial.model_dump(exclude={"skipped"}))
            for entry in partial.skipped:
                logger.warning("  skipped (discarded by rollback): %s", entry)
        active_session.rollback()
        raise
    finally:
        client.close()
        if owns_session:
            active_session.close()
