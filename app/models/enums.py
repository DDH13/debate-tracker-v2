from enum import Enum


class Side(str, Enum):
    PROP = "prop"
    OPP = "opp"


class SpeakerPosition(int, Enum):
    FIRST = 1
    SECOND = 2
    THIRD = 3
    REPLY = 4


class UserRole(str, Enum):
    BOARD = "board"
    VOLUNTEER = "volunteer"


class InstitutionKind(str, Enum):
    SCHOOL = "school"
    UNIVERSITY = "university"
    OTHER = "other"


class ContactRole(str, Enum):
    TEACHER_IN_CHARGE = "teacher_in_charge"
    COACH = "coach"
    POINT_OF_CONTACT = "point_of_contact"
    PRINCIPAL = "principal"
    # A student representative: debating captain, society president, etc. The exact
    # position goes in SchoolContact.title.
    STUDENT = "student"
    OTHER = "other"


class FeedbackSentiment(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class FeedbackStatus(str, Enum):
    OPEN = "open"
    ADDRESSED = "addressed"


class LeadChannel(str, Enum):
    EMAIL = "email"
    PHONE = "phone"
    SOCIAL_MEDIA = "social_media"
    EVENT = "event"
    REFERRAL = "referral"
    OTHER = "other"


class LeadStatus(str, Enum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    JOINED = "joined"
    DECLINED = "declined"
    NO_RESPONSE = "no_response"
