"""Plain create / list / get / update / delete routes for admin resources.

The management console has many small tables whose endpoints differ only in their models,
filters and parents, so they're generated here instead of hand-written per resource. Each
route follows the same patterns as the hand-written routers: `get_or_404`, `IntegrityError`
→ 409, `limit` capped at 100. Endpoints that compute something (summaries, conversions)
are still written by hand next to the generated ones.

No `from __future__ import annotations` in this module: the generated route functions
annotate their parameters with the schema classes passed in, and FastAPI needs those
annotations as real classes, not strings.
"""

from collections.abc import Callable, Sequence
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, Session, SQLModel, select

from app.api.deps import SessionDep, get_or_404


class ListParams(SQLModel):
    """Base for a resource's list filters. Every field a subclass adds is an equality
    filter on the model column of the same name, applied when it isn't None."""

    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=100)


_PAGING = {"offset", "limit"}

OnSave = Callable[[Session, Any], None]


def add_crud_routes(
    router: APIRouter,
    *,
    noun: str,
    model: type[SQLModel],
    create_schema: type[SQLModel],
    public_schema: type[SQLModel],
    update_schema: type[SQLModel],
    filters: type[ListParams] = ListParams,
    order_by: Sequence[Any] = (),
    parents: dict[str, type[SQLModel]] | None = None,
    conflict_detail: str = "Conflicts with an existing record",
    on_save: OnSave | None = None,
) -> None:
    """Registers `POST ""`, `GET ""`, `GET /{item_id}`, `PATCH /{item_id}` and
    `DELETE /{item_id}` on `router`.

    - `parents` maps FK fields to their model; a set FK pointing nowhere is a 404 (on create
      and update) instead of a database FK error.
    - `on_save(session, obj)` runs before every create/update commit, for validation
      (raise `HTTPException`) or side effects on other rows.
    """
    parents = parents or {}
    columns = model.__table__.columns  # type: ignore[attr-defined]

    def check_parents(session: Session, values: dict[str, Any]) -> None:
        for field, parent in parents.items():
            if values.get(field) is not None:
                get_or_404(session, parent, values[field])

    def save(session: Session, obj: SQLModel) -> SQLModel:
        if on_save is not None:
            on_save(session, obj)
        session.add(obj)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=conflict_detail) from exc
        session.refresh(obj)
        return obj

    def create_item(item_in: create_schema, session: SessionDep) -> Any:  # type: ignore[valid-type]
        check_parents(session, item_in.model_dump())
        return save(session, model.model_validate(item_in))

    def list_items(session: SessionDep, params: Annotated[filters, Query()]) -> Any:  # type: ignore[valid-type]
        statement = select(model)
        for field, value in params.model_dump(exclude=_PAGING, exclude_none=True).items():
            statement = statement.where(getattr(model, field) == value)
        statement = (
            statement.order_by(*order_by, model.id)  # type: ignore[attr-defined]
            .offset(params.offset)
            .limit(params.limit)
        )
        return list(session.exec(statement).all())

    def get_item(item_id: int, session: SessionDep) -> Any:
        return get_or_404(session, model, item_id)

    def update_item(item_id: int, item_in: update_schema, session: SessionDep) -> Any:  # type: ignore[valid-type]
        obj = get_or_404(session, model, item_id)
        changes = item_in.model_dump(exclude_unset=True)
        # Update schemas make every field optional, so an explicit null on a NOT NULL
        # column has to be caught here rather than surfacing as a misleading 409.
        nulled = [f for f, v in changes.items() if v is None and not columns[f].nullable]
        if nulled:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{', '.join(nulled)} cannot be null",
            )
        check_parents(session, changes)
        obj.sqlmodel_update(changes)
        return save(session, obj)

    def delete_item(item_id: int, session: SessionDep) -> None:
        obj = get_or_404(session, model, item_id)
        session.delete(obj)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{model.__name__} {item_id} is still referenced by other records",
            ) from exc

    router.add_api_route(
        "", create_item, methods=["POST"], response_model=public_schema,
        status_code=status.HTTP_201_CREATED, name=f"create_{noun}",
    )
    router.add_api_route(
        "", list_items, methods=["GET"], response_model=list[public_schema], name=f"list_{noun}s"
    )
    router.add_api_route(
        "/{item_id}", get_item, methods=["GET"], response_model=public_schema, name=f"get_{noun}"
    )
    router.add_api_route(
        "/{item_id}", update_item, methods=["PATCH"], response_model=public_schema,
        name=f"update_{noun}",
    )
    router.add_api_route(
        "/{item_id}", delete_item, methods=["DELETE"],
        status_code=status.HTTP_204_NO_CONTENT, name=f"delete_{noun}",
    )
