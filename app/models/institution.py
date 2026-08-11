from typing import TYPE_CHECKING

from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.models.debater import Debater
    from app.models.judge import Judge
    from app.models.team import Team


class InstitutionBase(SQLModel):
    name: str = Field(unique=True, index=True)
    code: str | None = None


class Institution(InstitutionBase, table=True):
    id: int | None = Field(default=None, primary_key=True)

    teams: list["Team"] = Relationship(back_populates="institution")
    debaters: list["Debater"] = Relationship(back_populates="institution")
    judges: list["Judge"] = Relationship(back_populates="institution")


class InstitutionCreate(InstitutionBase):
    pass


class InstitutionPublic(InstitutionBase):
    id: int


class InstitutionUpdate(SQLModel):
    name: str | None = None
    code: str | None = None
