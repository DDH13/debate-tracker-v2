from sqlmodel import Field, SQLModel

from app.models.enums import ContactRole


class SchoolContactBase(SQLModel):
    name: str = Field(min_length=1)
    role: ContactRole = ContactRole.POINT_OF_CONTACT
    # Their position in their own words, e.g. "President of the Senior English Debating
    # Society"; `role` is the coarse category.
    title: str | None = None
    email: str | None = None
    phone: str | None = None
    secondary_email: str | None = None
    secondary_phone: str | None = None
    is_primary: bool = False
    active: bool = True
    notes: str | None = None


class SchoolContact(SchoolContactBase, table=True):
    id: int | None = Field(default=None, primary_key=True)
    institution_id: int = Field(foreign_key="institution.id", index=True)


class SchoolContactCreate(SchoolContactBase):
    institution_id: int


class SchoolContactPublic(SchoolContactBase):
    id: int
    institution_id: int


class SchoolContactUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1)
    role: ContactRole | None = None
    title: str | None = None
    email: str | None = None
    phone: str | None = None
    secondary_email: str | None = None
    secondary_phone: str | None = None
    is_primary: bool | None = None
    active: bool | None = None
    notes: str | None = None
