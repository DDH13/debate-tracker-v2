from typing import TYPE_CHECKING

from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.models.round import Round


class MotionBase(SQLModel):
    text: str
    info_slide: str | None = None


class Motion(MotionBase, table=True):
    id: int | None = Field(default=None, primary_key=True)
    round_id: int = Field(foreign_key="round.id", unique=True)

    round: "Round" = Relationship(back_populates="motion")


class MotionCreate(MotionBase):
    pass


class MotionPublic(MotionBase):
    id: int
    round_id: int


class MotionUpdate(SQLModel):
    text: str | None = None
    info_slide: str | None = None
