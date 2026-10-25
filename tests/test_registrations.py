from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from app.models import (
    ContactRole,
    Institution,
    InstitutionKind,
    LeadStatus,
    Membership,
    OutreachLead,
    SchoolContact,
)
from app.services.registrations import (
    RegistrationImportError,
    import_registrations,
    normalize_phone,
    parse_registrations,
)

# The DC's actual form headers (2025), with made-up responses.
HEADER = (
    "Timestamp,Email Address,Name of the Institution,Name of the Representative,"
    "Representative's Affiliation to Institution,Contact No. (WhatsApp),"
    "Secondary Whatsapp Contact No. (Optional),Primary Email,Secondary Email (Optional),"
    "Proof of payment of Member Registration Fee (In PDF format),"
    "Signed letter requesting membership by the Teacher-In-Charge or any person holding a "
    "similar position within the institution (in PDF format)"
)
ROWS = [
    # Exact match for an existing school, curly apostrophe + trailing space.
    "9/25/2024 18:33:49,cap@example.com,St. Peter’s College ,Ann Captain,Debater (captain),"
    "0770000001,,club@example.com,,https://drive.google.com/open?id=p1,https://drive.google.com/open?id=l1",
    # The same school again, later, by the teacher in charge.
    "2/15/2025 19:21:24,t@example.com,St Peters College,mrs teacher,Teacher- In- Charge,"
    "+94 70 000 0002,-,Teacher@Example.com,,https://drive.google.com/open?id=p2,https://drive.google.com/open?id=l2",
    # Nothing like it in the DB: becomes a new school.
    "1/5/2025 10:57:42,x@example.com,BRAND NEW ACADEMY,Bo Student,President of the Debating Society,"
    "‪+94 71 000 0003‬,074 000 0004,bo@example.com,,https://drive.google.com/open?id=p3,",
    # Fuzzy-similar to an existing school but not equal: ambiguous until mapped.
    "3/5/2025 17:23:05,w@example.com,Wesley College Colombo,Wes Debater,debater,0770000005,,"
    "w@example.com,,https://drive.google.com/open?id=p4,https://drive.google.com/open?id=l4",
]


def _csv(*rows: str) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


@pytest.fixture
def schools(session: Session) -> dict[str, Institution]:
    peters = Institution(name="St. Peter's College")
    wesley = Institution(name="Wesley College", kind=InstitutionKind.OTHER)
    session.add_all([peters, wesley])
    session.commit()
    return {"peters": peters, "wesley": wesley}


# --- parsing ---------------------------------------------------------------------------


def test_parse_cleans_names_phones_emails_and_roles() -> None:
    regs = parse_registrations(_csv(*ROWS))
    assert [r.row for r in regs] == [2, 3, 4, 5]

    first, teacher, new, wesley = regs
    assert first.institution_name == "St. Peter's College"
    assert first.submitted_at.date() == date(2024, 9, 25)
    assert (first.role, first.rep_title) == (ContactRole.STUDENT, "Debater (captain)")
    assert first.phone == "+94770000001"
    assert first.payment_proof_url == "https://drive.google.com/open?id=p1"

    assert teacher.rep_name == "Mrs Teacher"  # all-lowercase names are title-cased
    assert teacher.role == ContactRole.TEACHER_IN_CHARGE
    assert teacher.rep_title is None  # "Teacher- In- Charge" only restates the role
    assert teacher.email == "teacher@example.com"
    assert teacher.secondary_phone is None  # "-" placeholder

    assert new.phone == "+94710000003"  # bidi marks stripped
    assert new.secondary_phone == "+94740000004"
    assert new.request_letter_url is None
    assert wesley.role == ContactRole.STUDENT


def test_parse_rejects_bad_input() -> None:
    with pytest.raises(RegistrationImportError, match="Missing form columns"):
        parse_registrations("Timestamp,Something else\n1/1/2025 1:00:00,x\n")
    with pytest.raises(RegistrationImportError, match="Row 2: can't read timestamp"):
        parse_registrations(_csv("yesterday," + ROWS[0].split(",", 1)[1]))
    with pytest.raises(RegistrationImportError, match="not an http"):
        parse_registrations(_csv(ROWS[0].replace("https://drive.google.com/open?id=p1", "javascript:alert(1)")))
    # Blank lines are fine.
    assert len(parse_registrations(_csv(ROWS[0], ",,,,,,,,,,"))) == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0700000006", "+94700000006"), ("+44 20 7946 0958", "+442079460958"), ("", None), ("n/a", None)],
)
def test_normalize_phone(raw, expected) -> None:
    assert normalize_phone(raw) == expected


# --- resolution ------------------------------------------------------------------------


def test_ambiguous_rows_block_the_import(session: Session, schools) -> None:
    report = import_registrations(session, parse_registrations(_csv(*ROWS)), 2025, apply=True)
    assert not report.applied
    [ambiguous] = report.ambiguous
    assert ambiguous.sheet_name == "Wesley College Colombo"
    assert ambiguous.suggestions[0].id == schools["wesley"].id
    resolutions = {r.sheet_name: r.resolution for r in report.rows}
    assert resolutions["BRAND NEW ACADEMY"] == "new"
    assert resolutions["St Peters College"] == "exact"
    assert session.exec(select(Membership)).all() == []


def test_mapping_to_unknown_institution_is_an_error(session: Session, schools) -> None:
    with pytest.raises(RegistrationImportError, match="doesn't exist"):
        import_registrations(
            session, parse_registrations(_csv(ROWS[3])), 2025, mapping={"Wesley College Colombo": 999}
        )


# --- applying --------------------------------------------------------------------------


def test_apply_creates_memberships_contacts_and_schools(session: Session, schools) -> None:
    report = import_registrations(
        session,
        parse_registrations(_csv(*ROWS)),
        2025,
        mapping={"WESLEY COLLEGE COLOMBO": schools["wesley"].id},  # matched by key, any case
        fee_lkr=Decimal("5000"),
        paid_on_submission=True,
        apply=True,
    )
    assert report.applied
    assert (report.schools, report.institutions_created) == (3, 1)
    assert (report.memberships_created, report.contacts_created) == (3, 4)

    peters = schools["peters"]
    session.refresh(peters)
    assert peters.kind == InstitutionKind.SCHOOL  # unset kind becomes school
    assert session.get(Institution, schools["wesley"].id).kind == InstitutionKind.OTHER  # explicit kept

    [membership] = session.exec(select(Membership).where(Membership.institution_id == peters.id)).all()
    # Two submissions collapse into one membership dated by the earliest.
    assert membership.registered_on == date(2024, 9, 25)
    assert membership.paid_on == date(2024, 9, 25)
    assert membership.fee_lkr == Decimal("5000")
    assert membership.payment_proof_url.endswith("p1")
    assert "Also submitted 2025-02-15 by Mrs Teacher" in membership.notes

    contacts = {c.name: c for c in session.exec(select(SchoolContact).where(SchoolContact.institution_id == peters.id))}
    assert set(contacts) == {"Ann Captain", "Mrs Teacher"}
    # The teacher in charge is preferred as primary over the earlier student.
    assert contacts["Mrs Teacher"].is_primary and not contacts["Ann Captain"].is_primary

    new_school = session.exec(select(Institution).where(Institution.name == "Brand New Academy")).one()
    assert new_school.kind == InstitutionKind.SCHOOL
    new_membership = session.exec(select(Membership).where(Membership.institution_id == new_school.id)).one()
    assert new_membership.request_letter_url is None


def test_dry_run_writes_nothing_but_reports_the_same(session: Session, schools) -> None:
    mapping = {"Wesley College Colombo": "new"}
    dry = import_registrations(session, parse_registrations(_csv(*ROWS)), 2025, mapping=mapping)
    assert not dry.applied
    assert (dry.institutions_created, dry.memberships_created, dry.contacts_created) == (2, 3, 4)
    assert session.exec(select(Membership)).all() == []
    assert len(session.exec(select(Institution)).all()) == 2

    real = import_registrations(session, parse_registrations(_csv(*ROWS)), 2025, mapping=mapping, apply=True)
    assert (real.institutions_created, real.memberships_created, real.contacts_created) == (2, 3, 4)


def test_reimport_is_a_no_op_and_fills_blanks_only(session: Session, schools) -> None:
    peters = schools["peters"]
    existing = Membership(institution_id=peters.id, year=2025, reason="Already recorded", fee_lkr=Decimal("1"))
    session.add(existing)
    session.add(SchoolContact(institution_id=peters.id, name="ANN CAPTAIN", is_primary=True))
    session.commit()

    mapping = {"Wesley College Colombo": schools["wesley"].id}
    first = import_registrations(session, parse_registrations(_csv(*ROWS)), 2025, mapping=mapping, apply=True)
    assert first.memberships_updated == 1
    assert first.contacts_updated == 1  # Ann's blanks filled, no duplicate contact
    session.refresh(existing)
    assert existing.reason == "Already recorded" and existing.fee_lkr == Decimal("1")  # not overwritten
    assert existing.registered_on == date(2024, 9, 25)  # blank filled
    contacts = session.exec(select(SchoolContact).where(SchoolContact.institution_id == peters.id)).all()
    assert len(contacts) == 2
    assert sum(c.is_primary for c in contacts) == 1  # existing primary kept

    again = import_registrations(session, parse_registrations(_csv(*ROWS)), 2025, mapping=mapping, apply=True)
    assert (again.institutions_created, again.memberships_created, again.memberships_updated) == (0, 0, 0)
    assert (again.contacts_created, again.contacts_updated) == (0, 0)


def test_open_outreach_leads_are_closed(session: Session, schools) -> None:
    peters = schools["peters"]
    session.add(OutreachLead(institution_id=peters.id, status=LeadStatus.IN_PROGRESS))
    session.add(OutreachLead(institution_id=peters.id, status=LeadStatus.DECLINED))
    session.commit()
    report = import_registrations(
        session, parse_registrations(_csv(ROWS[0])), 2025, apply=True
    )
    assert report.leads_closed == 1
    statuses = sorted(lead.status.value for lead in session.exec(select(OutreachLead)))
    assert statuses == ["declined", "joined"]


def test_reimport_with_new_mapping_links_the_school_created_last_time(session: Session, schools) -> None:
    # "new" means "don't use the fuzzy suggestions"; once the first run has created the
    # school, a re-run must link to it by exact name instead of creating it again.
    mapping = {"Wesley College Colombo": "new"}
    first = import_registrations(session, parse_registrations(_csv(*ROWS)), 2025, mapping=mapping, apply=True)
    assert first.institutions_created == 2

    again = import_registrations(session, parse_registrations(_csv(*ROWS)), 2025, mapping=mapping, apply=True)
    assert again.applied
    assert (again.institutions_created, again.memberships_created, again.contacts_created) == (0, 0, 0)
    resolutions = {r.sheet_name: r.resolution for r in again.rows}
    assert resolutions["Wesley College Colombo"] == "exact"
    assert resolutions["BRAND NEW ACADEMY"] == "exact"
