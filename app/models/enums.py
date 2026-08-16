from enum import Enum


class Side(str, Enum):
    PROP = "prop"
    OPP = "opp"


class SpeakerPosition(int, Enum):
    FIRST = 1
    SECOND = 2
    THIRD = 3
    REPLY = 4


class DebateFormat(str, Enum):
    TWO_TEAM = "two_team"
    BP = "bp"


class BPSide(str, Enum):
    OG = "og"
    OO = "oo"
    CG = "cg"
    CO = "co"


class BPPosition(int, Enum):
    FIRST = 1
    SECOND = 2
