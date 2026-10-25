"""Import a year's school registrations from the DC's Google Form responses (CSV export).

One form row = one school registering for the year. Applying an import:

- resolves each row to an institution — exact `institution_key` match, else an entry in
  the caller's mapping (`{"<sheet name>": <institution id> | "new"}`), else a new
  institution when nothing is even similar. A row with fuzzy candidates but no mapping is
  *ambiguous* and blocks the import: Jaro-Winkler is never auto-applied (see
  `app/services/institutions.py`);
- gives each school one `Membership` for the year (several rows for one school, e.g. a
  captain and a teacher both submitting, collapse into it), filling blanks on an existing
  membership but never overwriting;
- adds each row's representative as a `SchoolContact` unless someone with the same name is
  already listed, in which case only their blank fields are filled;
- marks the school's open outreach leads as joined.

Re-running the same sheet is therefore a no-op. Like the merge service, this raises its own
errors and never `HTTPException`; callers decide how to report them.
"""

import csv
import io
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from sqlmodel import Session, SQLModel, select

from app.models import (
    ContactRole,
    Institution,
    InstitutionKind,
    LeadStatus,
    Membership,
    OutreachLead,
    SchoolContact,
)
from app.models.membership import http_url
from app.services.institutions import SimilarInstitution, find_matching_institution, similar_institutions
from app.services.text import institution_key, name_key, normalize_text, tidy_case


class RegistrationImportError(Exception):
    pass


# Logical field → the start of the form question's header, matched case-insensitively so
# small wording edits to the form between years don't break the import.
_HEADERS = {
    "submitted_at": "timestamp",
    "submitter_email": "email address",
    "institution": "name of the institution",
    "rep_name": "name of the representative",
    "rep_affiliation": "representative's affiliation",
    "phone": "contact no",
    "secondary_phone": "secondary whatsapp",
    "email": "primary email",
    "secondary_email": "secondary email",
    "payment_proof": "proof of payment",
    "request_letter": "signed letter",
}
_REQUIRED = ("submitted_at", "institution", "rep_name")
_TIMESTAMP_FORMATS = ("%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S")
_STUDENT_WORDS = ("debater", "debator", "captain", "president", "treasurer", "secretary", "student", "member")


@dataclass
class Registration:
    row: int  # spreadsheet row number (the header is row 1)
    submitted_at: datetime
    institution_name: str
    rep_name: str
    rep_title: str | None
    role: ContactRole
    submitter_email: str | None
    phone: str | None
    secondary_phone: str | None
    email: str | None
    secondary_email: str | None
    payment_proof_url: str | None
    request_letter_url: str | None


def normalize_phone(raw: str | None) -> str | None:
    """Digits with a leading `+`; Sri Lankan local numbers (`07x…`, `94…`) become `+94…`.
    Placeholders with no digits (`-`) become None."""
    if not raw:
        return None
    # Drop invisible formatting characters (bidi marks pasted from WhatsApp, etc.).
    text = "".join(ch for ch in raw if unicodedata.category(ch) != "Cf")
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    if text.strip().startswith("+"):
        return f"+{digits}"
    if len(digits) == 10 and digits.startswith("0"):
        return f"+94{digits[1:]}"
    if len(digits) == 11 and digits.startswith("94"):
        return f"+{digits}"
    return digits


def _email(raw: str | None) -> str | None:
    value = normalize_text(raw).lower()
    return value or None


def _role_for(affiliation: str) -> ContactRole:
    text = affiliation.lower()
    if "teacher" in text:
        return ContactRole.TEACHER_IN_CHARGE
    if "coach" in text:
        return ContactRole.COACH
    if "principal" in text:
        return ContactRole.PRINCIPAL
    if any(word in text for word in _STUDENT_WORDS):
        return ContactRole.STUDENT
    return ContactRole.POINT_OF_CONTACT if not text else ContactRole.OTHER


def _title(affiliation: str, role: ContactRole) -> str | None:
    """The free-text position, unless it only restates the role ("Teacher- In- Charge")."""
    if not affiliation or institution_key(affiliation) == institution_key(role.value.replace("_", " ")):
        return None
    return affiliation


def _parse_timestamp(raw: str, row: int) -> datetime:
    value = normalize_text(raw)
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise RegistrationImportError(f"Row {row}: can't read timestamp {raw!r}")


def _link(raw: str | None, row: int, label: str) -> str | None:
    try:
        return http_url(normalize_text(raw))
    except ValueError as exc:
        raise RegistrationImportError(f"Row {row}: {label} {raw!r} is not an http(s) link") from exc


def parse_registrations(text: str) -> list[Registration]:
    """Rows of a Google Form CSV export. Blank rows are skipped; a row missing the school or
    representative name is an error rather than silently dropped."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise RegistrationImportError("The CSV is empty") from exc

    columns: dict[str, int] = {}
    for logical, prefix in _HEADERS.items():
        for index, title in enumerate(header):
            if normalize_text(title).lower().startswith(prefix):
                columns[logical] = index
                break
    missing = [_HEADERS[f] for f in _REQUIRED if f not in columns]
    if missing:
        raise RegistrationImportError(f"Missing form columns: {', '.join(missing)}")

    registrations = []
    for row_number, cells in enumerate(reader, start=2):
        if not any(c.strip() for c in cells):
            continue

        def cell(logical: str) -> str:
            index = columns.get(logical)
            return cells[index] if index is not None and index < len(cells) else ""

        institution = normalize_text(cell("institution"))
        rep_name = normalize_text(cell("rep_name"))
        if not institution or not rep_name:
            raise RegistrationImportError(f"Row {row_number}: missing the school or representative name")
        affiliation = normalize_text(cell("rep_affiliation"))
        role = _role_for(affiliation)
        registrations.append(
            Registration(
                row=row_number,
                submitted_at=_parse_timestamp(cell("submitted_at"), row_number),
                institution_name=institution,
                rep_name=tidy_case(rep_name),
                rep_title=_title(affiliation, role),
                role=role,
                submitter_email=_email(cell("submitter_email")),
                phone=normalize_phone(cell("phone")),
                secondary_phone=normalize_phone(cell("secondary_phone")),
                email=_email(cell("email")),
                secondary_email=_email(cell("secondary_email")),
                payment_proof_url=_link(cell("payment_proof"), row_number, "payment proof"),
                request_letter_url=_link(cell("request_letter"), row_number, "request letter"),
            )
        )
    return registrations


# --- Resolution and report ----------------------------------------------------------

Resolution = Literal["exact", "mapped", "new", "ambiguous"]


class RowPlan(SQLModel):
    row: int
    sheet_name: str
    resolution: Resolution
    institution_id: int | None
    institution_name: str | None
    suggestions: list[SimilarInstitution] = []


class RegistrationImportReport(SQLModel):
    year: int
    applied: bool
    rows: list[RowPlan]
    schools: int = 0
    institutions_created: int = 0
    memberships_created: int = 0
    memberships_updated: int = 0
    contacts_created: int = 0
    contacts_updated: int = 0
    leads_closed: int = 0

    @property
    def ambiguous(self) -> list[RowPlan]:
        return [r for r in self.rows if r.resolution == "ambiguous"]


Mapping = dict[str, int | Literal["new"]]


def _resolve(session: Session, registrations: list[Registration], mapping: Mapping) -> list[RowPlan]:
    by_key = {institution_key(name): target for name, target in mapping.items()}
    plans = []
    for reg in registrations:
        key = institution_key(reg.institution_name)
        target = by_key.get(key)
        # An explicit id always wins. Otherwise an exact name match comes first, even over a
        # "new" mapping: "new" only means "not one of the fuzzy suggestions", and on a re-run
        # the school a previous run created is now an exact match.
        if target is not None and target != "new":
            institution = session.get(Institution, target)
            if institution is None:
                raise RegistrationImportError(
                    f"Mapping for {reg.institution_name!r} points at institution {target}, which doesn't exist"
                )
            plans.append(RowPlan(row=reg.row, sheet_name=reg.institution_name, resolution="mapped",
                                 institution_id=institution.id, institution_name=institution.name))
            continue
        institution = find_matching_institution(session, reg.institution_name)
        if institution is not None:
            plans.append(RowPlan(row=reg.row, sheet_name=reg.institution_name, resolution="exact",
                                 institution_id=institution.id, institution_name=institution.name))
            continue
        if target == "new":
            plans.append(RowPlan(row=reg.row, sheet_name=reg.institution_name, resolution="new",
                                 institution_id=None, institution_name=tidy_case(reg.institution_name)))
            continue
        suggestions = similar_institutions(session, reg.institution_name)[:3]
        plans.append(
            RowPlan(
                row=reg.row,
                sheet_name=reg.institution_name,
                resolution="ambiguous" if suggestions else "new",
                institution_id=None,
                institution_name=None if suggestions else tidy_case(reg.institution_name),
                suggestions=suggestions,
            )
        )
    return plans


def _fill_blanks(obj: SQLModel, values: dict) -> bool:
    """Set each attribute that is currently empty; return whether anything changed."""
    changed = False
    for attr, value in values.items():
        if value is not None and getattr(obj, attr) in (None, ""):
            setattr(obj, attr, value)
            changed = True
    return changed


def import_registrations(
    session: Session,
    registrations: list[Registration],
    year: int,
    *,
    mapping: Mapping | None = None,
    fee_lkr: Decimal | None = None,
    paid_on_submission: bool = False,
    apply: bool = False,
) -> RegistrationImportReport:
    """Plans and performs the import inside one transaction, then commits when `apply` is
    set or rolls back otherwise — so a dry run reports exactly what applying would do.
    Ambiguous rows are reported and nothing is written; resolve them via `mapping`."""
    plans = _resolve(session, registrations, mapping or {})
    report = RegistrationImportReport(year=year, applied=False, rows=plans)
    if report.ambiguous:
        return report

    try:
        # New institutions are keyed by name so several rows for one new school share it.
        created: dict[str, Institution] = {}
        groups: dict[int, list[Registration]] = defaultdict(list)
        for plan, reg in zip(plans, registrations):
            if plan.institution_id is None:
                key = institution_key(plan.institution_name)
                if key not in created:
                    created[key] = Institution(name=plan.institution_name, kind=InstitutionKind.SCHOOL)
                    session.add(created[key])
                    session.flush()
                    report.institutions_created += 1
                plan.institution_id = created[key].id
            groups[plan.institution_id].append(reg)

        for institution_id, regs in groups.items():
            regs.sort(key=lambda r: r.submitted_at)
            institution = session.get(Institution, institution_id)
            if institution.kind is None:
                institution.kind = InstitutionKind.SCHOOL
                session.add(institution)
            _apply_membership(session, report, institution_id, year, regs, fee_lkr, paid_on_submission)
            _apply_contacts(session, report, institution_id, regs)
            for lead in session.exec(
                select(OutreachLead).where(
                    OutreachLead.institution_id == institution_id,
                    OutreachLead.status.in_([LeadStatus.NEW, LeadStatus.IN_PROGRESS]),
                )
            ).all():
                lead.status = LeadStatus.JOINED
                session.add(lead)
                report.leads_closed += 1
        report.schools = len(groups)

        session.flush()
        if apply:
            session.commit()
            report.applied = True
        else:
            session.rollback()
    except Exception:
        session.rollback()
        raise
    return report


def _apply_membership(
    session: Session,
    report: RegistrationImportReport,
    institution_id: int,
    year: int,
    regs: list[Registration],
    fee_lkr: Decimal | None,
    paid_on_submission: bool,
) -> None:
    first = regs[0]
    proof = next((r.payment_proof_url for r in regs if r.payment_proof_url), None)
    letter = next((r.request_letter_url for r in regs if r.request_letter_url), None)
    values = {
        "registered_on": first.submitted_at.date(),
        "fee_lkr": fee_lkr,
        "paid_on": first.submitted_at.date() if paid_on_submission and proof else None,
        "payment_proof_url": proof,
        "request_letter_url": letter,
    }
    membership = session.exec(
        select(Membership).where(Membership.institution_id == institution_id, Membership.year == year)
    ).first()
    if membership is not None:
        if _fill_blanks(membership, values):
            session.add(membership)
            report.memberships_updated += 1
        return

    notes = [f"Imported from the {year} registration form."]
    for extra in regs[1:]:
        notes.append(
            f"Also submitted {extra.submitted_at.date().isoformat()} by {extra.rep_name}"
            + (f" (proof: {extra.payment_proof_url})" if extra.payment_proof_url not in (None, proof) else "")
        )
    session.add(Membership(institution_id=institution_id, year=year, notes=" ".join(notes), **values))
    report.memberships_created += 1


def _apply_contacts(
    session: Session, report: RegistrationImportReport, institution_id: int, regs: list[Registration]
) -> None:
    contacts = {
        name_key(c.name): c
        for c in session.exec(select(SchoolContact).where(SchoolContact.institution_id == institution_id)).all()
    }
    has_primary = any(c.is_primary for c in contacts.values())
    new_contacts: list[SchoolContact] = []
    for reg in regs:
        values = {
            "title": reg.rep_title,
            "email": reg.email,
            "phone": reg.phone,
            "secondary_email": reg.secondary_email,
            "secondary_phone": reg.secondary_phone,
        }
        existing = contacts.get(name_key(reg.rep_name))
        if existing is not None:
            if existing.id is not None and _fill_blanks(existing, values):
                session.add(existing)
                report.contacts_updated += 1
            continue
        contact = SchoolContact(institution_id=institution_id, name=reg.rep_name, role=reg.role, **values)
        session.add(contact)
        contacts[name_key(reg.rep_name)] = contact
        new_contacts.append(contact)
        report.contacts_created += 1

    # One registered representative becomes the primary contact if the school has none —
    # a teacher in charge over a student, else the earliest submission.
    if not has_primary and new_contacts:
        primary = next((c for c in new_contacts if c.role == ContactRole.TEACHER_IN_CHARGE), new_contacts[0])
        primary.is_primary = True
