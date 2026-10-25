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


class PersonKind(str, Enum):
    BOARD = "board"
    VOLUNTEER = "volunteer"
    STAFF = "staff"
    OTHER = "other"


class AccountKind(str, Enum):
    BANK = "bank"
    CASH = "cash"
    MOBILE_WALLET = "mobile_wallet"
    OTHER = "other"


class TransactionKind(str, Enum):
    INCOME = "income"
    EXPENSE = "expense"
    TRANSFER = "transfer"


class PaymentMethod(str, Enum):
    BANK_TRANSFER = "bank_transfer"
    CASH = "cash"
    CARD = "card"
    CHEQUE = "cheque"
    ONLINE = "online"
    OTHER = "other"


class ReimbursementStatus(str, Enum):
    NONE = "none"
    # Someone paid out of their own pocket and the DC hasn't paid them back yet.
    PENDING = "pending"
    REIMBURSED = "reimbursed"


class BillingCycle(str, Enum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"


class SquadKind(str, Enum):
    NATIONAL = "national"
    DEVELOPMENT = "development"


class SquadRole(str, Enum):
    CAPTAIN = "captain"
    MEMBER = "member"
    RESERVE = "reserve"


class TripStatus(str, Enum):
    PLANNING = "planning"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TripCostCategory(str, Enum):
    FLIGHTS = "flights"
    VISA = "visa"
    ACCOMMODATION = "accommodation"
    REGISTRATION = "registration"
    FOOD = "food"
    LOCAL_TRANSPORT = "local_transport"
    INSURANCE = "insurance"
    OTHER = "other"
