from sqlmodel import SQLModel


class ParticipantBase(SQLModel):
    name: str
    email: str | None = None
