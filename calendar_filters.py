"""Structured Calendar filters over what the Google Calendar API actually returns.

What exists in Google Calendar (API revision 2026-10):
- Labels: an event's `eventLabelId` refers to a label (id, name, colour) defined on its calendar
  (`labelProperties.eventLabels`, read with calendars.get). The id comes with every event;
  the label *names* need the read-only calendar-settings permission (calendar.calendars.readonly),
  which K.R.I.S. requests only when [google] calendar_labels = on. Without it a label filter is
  refused with that explanation: it never falls back to a text search silently.
- Event type: `eventType` is one of default, focusTime, outOfOffice, workingLocation, birthday,
  fromGmail. "Meeting", "exam" or "lecture" are not types Google records.
- Colour: `colorId` (superseded by labels; colour names are not available with this permission).

So "meetings", "exams", "lectures", "labs" and "deadlines" (`kind`) are matched on documented
evidence and every result says which: for meetings, a video-call link or other attendees, or a
meeting word in the title; for the others, the listed words in the title or description.
`title_contains` matches the title only. Nothing here broadens a filter: an event either meets
the stated rule or is left out.
"""

from __future__ import annotations

import re

EVENT_TYPES = ("default", "focusTime", "outOfOffice", "workingLocation", "birthday", "fromGmail")
KIND_WORDS = {
    "meeting": ("meeting", "meet", "call", "sync", "standup", "stand-up", "stand up", "1:1", "one-on-one",
                "interview", "catch-up", "catch up"),
    "exam": ("exam", "exams", "examination", "midsem", "mid-sem", "mid sem", "midterm", "compre", "comprehensive",
             "endsem", "end-sem", "end sem", "final exam", "quiz", "viva"),
    "lecture": ("lecture", "lectures", "class", "tutorial", "seminar"),
    "lab": ("lab", "labs", "laboratory", "practical"),
    "deadline": ("deadline", "due", "submission", "submit"),
}
KINDS = tuple(KIND_WORDS)


def _found(words: tuple[str, ...], text: str) -> str | None:
    for word in words:
        if re.search(rf"(?<![\w]){re.escape(word)}(?![\w])", text, re.IGNORECASE):
            return word
    return None


def kind_reason(item: dict, kind: str) -> str | None:
    """Why a raw Google event counts as `kind`, or None if it does not."""
    title = str(item.get("summary") or "")
    if kind == "meeting":
        if item.get("hangoutLink") or (item.get("conferenceData") or {}).get("conferenceId"):
            return "has a video-call link"
        others = [a for a in item.get("attendees") or [] if isinstance(a, dict) and not a.get("self")]
        if others:
            return f"has {len(others)} other attendee(s)"
        word = _found(KIND_WORDS["meeting"], title)
        return f"title word '{word}'" if word else None
    word = _found(KIND_WORDS[kind], title)
    if word:
        return f"title word '{word}'"
    word = _found(KIND_WORDS[kind], str(item.get("description") or ""))
    return f"description word '{word}'" if word else None


def match(item: dict, *, kind: str | None = None, title_contains: str | None = None,
          label_id: str | None = None, label_name: str | None = None) -> list[str] | None:
    """The reasons a raw event meets every given filter, or None if any filter fails."""
    reasons = []
    if label_id is not None:
        if item.get("eventLabelId") != label_id:
            return None
        reasons.append(f"Google Calendar label '{label_name}'")
    if title_contains:
        if title_contains.casefold() not in str(item.get("summary") or "").casefold():
            return None
        reasons.append(f"title contains '{title_contains}'")
    if kind:
        reason = kind_reason(item, kind)
        if reason is None:
            return None
        reasons.append(reason)
    return reasons


def describe(kind: str | None, title_contains: str | None, label: str | None, event_type: str | None) -> str:
    """How the filters were applied, stated for the user."""
    parts = []
    if label:
        parts.append(f"Google Calendar label '{label}'")
    if event_type:
        parts.append(f"Google event type '{event_type}'")
    if title_contains:
        parts.append(f"title containing '{title_contains}'")
    if kind:
        how = ("a video-call link, other attendees, or a meeting word in the title" if kind == "meeting"
               else "these words in the title or description: " + ", ".join(KIND_WORDS[kind]))
        parts.append(f"{kind}s, recognised by {how} (Google Calendar has no '{kind}' type)")
    return "; ".join(parts)
