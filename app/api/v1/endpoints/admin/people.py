from fastapi import APIRouter

from app.api.crud import ListParams, add_crud_routes
from app.models import Person, PersonCreate, PersonKind, PersonPublic, PersonUpdate


class PersonFilters(ListParams):
    kind: PersonKind | None = None
    active: bool | None = None


router = APIRouter(prefix="/people", tags=["admin: people"])
add_crud_routes(
    router,
    noun="person",
    model=Person,
    create_schema=PersonCreate,
    public_schema=PersonPublic,
    update_schema=PersonUpdate,
    filters=PersonFilters,
    order_by=(Person.name,),
    conflict_detail="This person is still referenced (transactions, subscriptions); deactivate them instead",
)
