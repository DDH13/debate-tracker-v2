from fastapi import APIRouter, HTTPException, status
from sqlmodel import Session

from app.api.crud import ListParams, add_crud_routes
from app.api.deps import SessionDep, get_or_404
from app.models import Person, PersonCreate, PersonKind, PersonPublic, PersonUpdate
from app.services import people as people_service


class PersonFilters(ListParams):
    kind: PersonKind | None = None
    active: bool | None = None


def _guard_person_delete(session: Session, person: Person) -> None:
    references = people_service.person_references(session, person.id)
    if references:
        listed = ", ".join(f"{n} {label}" for label, n in references.items())
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete {person.name}: named on {listed}. Deactivate them instead.",
        )


router = APIRouter(prefix="/people", tags=["admin: people"])


# Before the generated `/{item_id}` routes.
@router.get("/{person_id}/detail", response_model=people_service.PersonDetail)
def person_detail(person_id: int, session: SessionDep) -> people_service.PersonDetail:
    """The person with their console account, board roles, tasks, workshops facilitated and
    the money they've paid (and are owed)."""
    return people_service.person_detail(session, get_or_404(session, Person, person_id))

add_crud_routes(
    router,
    noun="person",
    model=Person,
    create_schema=PersonCreate,
    public_schema=PersonPublic,
    update_schema=PersonUpdate,
    filters=PersonFilters,
    order_by=(Person.name,),
    conflict_detail="This person is still referenced; deactivate them instead",
    before_delete=_guard_person_delete,
)
