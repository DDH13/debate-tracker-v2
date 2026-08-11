from datetime import date

from sqlmodel import SQLModel


class ParticipantBase(SQLModel):
    full_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    birthdate: date | None = None
    gender: str | None = None
    
