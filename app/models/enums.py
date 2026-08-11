from enum import Enum


class Side(str, Enum):
    PROP = "prop"
    OPP = "opp"


class SpeakerPosition(int, Enum):
    FIRST = 1
    SECOND = 2
    THIRD = 3
    REPLY = 4
