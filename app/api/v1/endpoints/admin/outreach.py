from datetime import date
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, Session, SQLModel, select

from app.api.crud import ListParams, add_crud_routes
from app.api.deps import SessionDep, get_or_404
from app.models import (
    ContactRole,
    Institution,
    InstitutionKind,
    InstitutionPublic,
    LeadStatus,
    Membership,
    MembershipPublic,
    OutreachLead,
    OutreachLeadCreate,
    OutreachLeadPublic,
    OutreachLeadUpdate,
    SchoolContact,
)
from app.models.membership import YEAR_MAX, YEAR_MIN
from app.services.institutions import find_matching_institution
from app.services.text import name_key, normalize_text, tidy_case

router = APIRouter(prefix="/outreach", tags=["admin: outreach"])


class OutreachFilters(ListParams):
    status: LeadStatus | None = None
    institution_id: int | None = None


class OutreachConvert(SQLModel):
    """Overrides for the membership a converted lead gets. Everything is optional."""

    institution_id: int | None = None
    year: int | None = Field(default=None, ge=YEAR_MIN, le=YEAR_MAX)
    registered_on: date | None = None
    reason: str | None = None
    fee_lkr: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    paid_on: date | None = None


class OutreachConversion(SQLModel):
    lead: OutreachLeadPublic
    institution: InstitutionPublic
    membership: MembershipPublic
    institution_created: bool
    membership_created: bool
    contact_created: bool


def _require_school(_session: Session, lead: OutreachLead) -> None:
    if lead.institution_id is None and not normalize_text(lead.school_name):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A lead needs a school name or a linked school",
        )


@router.post("/{lead_id}/convert", response_model=OutreachConversion)
def convert_lead(
    lead_id: int, session: SessionDep, convert_in: OutreachConvert | None = None
) -> OutreachConversion:
    """Turns a lead into a member school in one commit:

    - the school is the one given, else the lead's linked one, else an existing institution
      whose name key matches the lead's school name, else a new institution;
    - the membership for the year is reused if it already exists;
    - the lead's contact is added as the school's point of contact unless someone with the
      same name is already listed.
    """
    convert_in = convert_in or OutreachConvert()
    lead = get_or_404(session, OutreachLead, lead_id)
    if lead.status == LeadStatus.JOINED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This lead has already joined")

    institution_created = False
    linked_id = convert_in.institution_id or lead.institution_id
    if linked_id is not None:
        institution = get_or_404(session, Institution, linked_id)
    else:
        school_name = normalize_text(lead.school_name)
        if not school_name:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="A lead needs a school name or a linked school",
            )
        institution = find_matching_institution(session, school_name)
        if institution is None:
            institution = Institution(name=tidy_case(school_name), kind=InstitutionKind.SCHOOL)
            session.add(institution)
            session.flush()
            institution_created = True
    if institution.kind is None:
        institution.kind = InstitutionKind.SCHOOL
        session.add(institution)

    year = convert_in.year or date.today().year
    membership = session.exec(
        select(Membership).where(Membership.institution_id == institution.id, Membership.year == year)
    ).first()
    membership_created = membership is None
    if membership is None:
        membership = Membership(
            institution_id=institution.id,
            year=year,
            registered_on=convert_in.registered_on or date.today(),
            reason=convert_in.reason,
            fee_lkr=convert_in.fee_lkr,
            paid_on=convert_in.paid_on,
        )
        session.add(membership)

    contact_created = False
    contact_name = normalize_text(lead.contact_name)
    if contact_name:
        existing = session.exec(
            select(SchoolContact).where(SchoolContact.institution_id == institution.id)
        ).all()
        if all(name_key(c.name) != name_key(contact_name) for c in existing):
            session.add(
                SchoolContact(
                    institution_id=institution.id,
                    name=contact_name,
                    role=ContactRole.POINT_OF_CONTACT,
                    email=lead.contact_email,
                    phone=lead.contact_phone,
                    is_primary=not any(c.is_primary for c in existing),
                )
            )
            contact_created = True

    lead.status = LeadStatus.JOINED
    lead.institution_id = institution.id
    session.add(lead)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Conversion conflicts with existing data"
        ) from exc
    for obj in (lead, institution, membership):
        session.refresh(obj)
    return OutreachConversion(
        lead=OutreachLeadPublic.model_validate(lead),
        institution=InstitutionPublic.model_validate(institution),
        membership=MembershipPublic.model_validate(membership),
        institution_created=institution_created,
        membership_created=membership_created,
        contact_created=contact_created,
    )


add_crud_routes(
    router,
    noun="outreach_lead",
    model=OutreachLead,
    create_schema=OutreachLeadCreate,
    public_schema=OutreachLeadPublic,
    update_schema=OutreachLeadUpdate,
    filters=OutreachFilters,
    order_by=(OutreachLead.created_at.desc(),),
    parents={"institution_id": Institution},
    on_save=_require_school,
)
