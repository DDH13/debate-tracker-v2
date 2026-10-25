from pydantic import field_validator
from sqlmodel import Field, SQLModel, UniqueConstraint

from app.models.enums import TransactionKind


def _income_or_expense(kind: TransactionKind | None) -> TransactionKind | None:
    if kind == TransactionKind.TRANSFER:
        raise ValueError("a category is for income or expenses, not transfers")
    return kind


class FinanceCategoryBase(SQLModel):
    name: str = Field(min_length=1)
    kind: TransactionKind
    active: bool = True

    _check_kind = field_validator("kind")(_income_or_expense)


class FinanceCategory(FinanceCategoryBase, table=True):
    __table_args__ = (UniqueConstraint("name", "kind"),)

    id: int | None = Field(default=None, primary_key=True)


class FinanceCategoryCreate(FinanceCategoryBase):
    pass


class FinanceCategoryPublic(FinanceCategoryBase):
    id: int


class FinanceCategoryUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1)
    kind: TransactionKind | None = None
    active: bool | None = None

    _check_kind = field_validator("kind")(_income_or_expense)
