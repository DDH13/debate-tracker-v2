from datetime import datetime, timezone

from pydantic import field_validator
from sqlmodel import Field, Relationship, SQLModel

from app.models.enums import UserRole


def normalize_email(email: str) -> str:
    return email.strip().lower()


class UserBase(SQLModel):
    email: str = Field(unique=True, index=True)
    name: str
    role: UserRole
    is_active: bool = True


class User(UserBase, table=True):
    id: int | None = Field(default=None, primary_key=True)
    password_hash: str
    # The DC person this account belongs to (needed for volunteers' tasks in a later phase).
    person_id: int | None = Field(default=None, foreign_key="person.id", ondelete="SET NULL")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    sessions: list["AuthSession"] = Relationship(
        back_populates="user",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class UserCreate(UserBase):
    password: str = Field(min_length=8)
    person_id: int | None = None

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email(value)


class UserPublic(UserBase):
    id: int
    person_id: int | None = None
    created_at: datetime


class UserUpdate(SQLModel):
    email: str | None = None
    name: str | None = None
    role: UserRole | None = None
    is_active: bool | None = None
    person_id: int | None = None

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str | None) -> str | None:
        return normalize_email(value) if value is not None else None


class PasswordReset(SQLModel):
    password: str = Field(min_length=8)


class AuthSession(SQLModel, table=True):
    """A signed-in browser. Only the sha256 of the cookie token is stored, so a leaked DB row
    can't be replayed as a session."""

    id: int | None = Field(default=None, primary_key=True)
    token_hash: str = Field(unique=True, index=True)
    user_id: int = Field(foreign_key="user.id", index=True, ondelete="CASCADE")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime

    user: User = Relationship(back_populates="sessions")
