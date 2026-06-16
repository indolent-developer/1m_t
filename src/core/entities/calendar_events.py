"""
core.entities.calendar_events

Event type enum for the economic / corporate calendar.
"""
from enum import Enum


class CalendarEventType(Enum):
    EARNINGS  = "earnings"
    DIVIDEND  = "dividend"
    SPLIT     = "split"
    IPO       = "ipo"
    MERGER    = "merger"
    FED_EVENT = "fed_event"
    OTHER     = "other"
