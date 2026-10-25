"""A caregiver checking in on someone, without the uAgents plumbing.

  caregiver: "How is Maria doing?"
  agent:     the security question on Maria's record
  caregiver: the answer, checked against the record
  agent:     what Maria is good with and struggles with, from caretaker notes and tracked runs
  caregiver: "Next time go slower on checkout pages."
  agent:     "Noted!", saved as a caretaker note

An answer passes only when nothing but filler words is left after the expected value is
removed, so "She lives in Plano" passes and a list of guesses doesn't. MAX_TRIES wrong
answers lock that person for the sender for an hour, and MAX_PERSON_FAILURES from anyone
lock them for everyone.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from shared import llm
from shared.security import clean_text, short_address

from caretaker.records import Note, Patient, Records, RecordsError, Run

MAX_TRIES = 3
MAX_PERSON_FAILURES = 10
LOCK_SECONDS = 3600
UNLOCK_SECONDS = 1800
MAX_SESSIONS = 2000
MAX_ITEMS = 4

UNAVAILABLE = "Records aren't available right now."
LOCKED = "Too many wrong answers. Try again in an hour."

RESET = {"reset", "start over", "cancel", "stop"}
STATUS = re.compile(r"\b(?:how(?:'s| is| are| has)|doing|update|status|check(?:ing)? (?:in|on)|tell me about)\b", re.I)
NOTE = re.compile(
    r"\b(?:next time|from now on|going forward|remember|make sure|note|please|try to|don'?t|do not|always|never|"
    r"instead|should|focus)\b",
    re.I,
)
THANKS = re.compile(r"^\s*(?:thanks?|thank you|ok(?:ay)?|great|got it|cool|perfect|bye|goodbye)\b[\s!.]*$", re.I)
MENTION = re.compile(r"@[A-Za-z0-9][\w.-]*")

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
FILLER = set(
    "a an the i it its s is was were he she they them his her their lives lived live living in on at from of "
    "that this think believe um uh my answer city town year month born birthday birth date day".split()
)
DATE = re.compile(r"([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})")

SUMMARY_SYSTEM = (
    "You tell a family caregiver how an older adult is managing websites and devices. Use only the facts given. "
    "Caregiver notes say what the helper was asked to do differently, so each one points to something the person "
    "finds hard. Finished runs point to things they manage. Reply with JSON "
    '{"good_with": [], "struggles_with": []}. Each item is a short lowercase phrase of 2 to 6 words that reads '
    'naturally after "is good with" or "struggles with". At most 4 items per list. Leave a list empty when no '
    "fact supports it. No medical advice."
)

Summarize = Callable[[str], Awaitable[dict]]


async def llm_summary(facts: str) -> dict:
    if not llm.configured():
        raise llm.LLMError("GROQ_API_KEY isn't set.")
    return await llm.complete_json(SUMMARY_SYSTEM, facts, max_tokens=600)


def strip_mentions(text: str) -> str:
    """ASI:One delivers the @handle the sender typed as part of the message."""
    return MENTION.sub(" ", text)


def _words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).split()


def answer_hash(patient_id: str, answer: str) -> str:
    """What patient_secret stores for an answer, compared the same way as any other answer."""
    return hashlib.sha256(f"{patient_id}:{' '.join(_words(answer))}".encode()).hexdigest()


def expected_answers(patient: Patient) -> list[list[str]]:
    """The accepted answers to the person's security question, as word lists. Empty if the record can't say."""
    question = patient.security_question.lower()
    date = DATE.search(patient.date_of_birth)
    month = date.group(1).lower() if date else ""
    month_number = str(MONTHS.index(month) + 1) if month in MONTHS else ""
    if "city" in question or "live" in question or "town" in question:
        return [_words(patient.city)] if _words(patient.city) else []
    if not date:
        return []
    if "year" in question:
        return [[date.group(3)]]
    if "month" in question:
        return [[month]] + ([[month_number]] if month_number else [])
    if "born" in question or "birth" in question:
        day, year = str(int(date.group(2))), date.group(3)
        return [[month, day, year]] + ([[month_number, day, year]] if month_number else [])
    return []


def is_correct(answer: str, patient: Patient) -> bool:
    ignore = FILLER | set(_words(patient.full_name))
    words = [w.removesuffix("th").removesuffix("st").removesuffix("nd").removesuffix("rd")
             if re.fullmatch(r"\d+(?:st|nd|rd|th)", w) else w for w in _words(answer)]
    words = [str(int(w)) if w.isdigit() and len(w) <= 2 else w for w in words if w not in ignore]
    if patient.answer_hash:
        return hmac.compare_digest(answer_hash(patient.patient_id, " ".join(words)), patient.answer_hash)
    return any(words == expected for expected in expected_answers(patient))


def has_answer(patient: Patient) -> bool:
    return bool(patient.security_question) and bool(patient.answer_hash or expected_answers(patient))


def find_patients(text: str, patients: list[Patient]) -> list[Patient]:
    full = [p for p in patients if p.full_name and re.search(rf"\b{re.escape(p.full_name)}\b", text, re.I)]
    if full:
        return full
    return [p for p in patients
            if any(len(part) > 2 and re.search(rf"\b{re.escape(part)}\b", text, re.I) for part in p.full_name.split())]


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def report(name: str, good: list[str], struggles: list[str]) -> str:
    if good and struggles:
        return f"{name} is good with {_join(good)}, and struggles with {_join(struggles)}."
    if good:
        return f"{name} is good with {_join(good)}."
    if struggles:
        return f"{name} struggles with {_join(struggles)}."
    return f"Nothing recorded for {name} yet."


def _items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = clean_text(item, 60).strip().rstrip(".")
        if text and text.lower() not in {i.lower() for i in items}:
            items.append(text[0].lower() + text[1:] if not text[:2].isupper() else text)
    return items[:MAX_ITEMS]


def facts(patient: Patient, notes: list[Note], runs: list[Run]) -> str:
    lines = [f"Name: {patient.full_name}"]
    if patient.conditions:
        lines.append(f"Conditions: {patient.conditions}")
    if notes:
        lines.append("Caregiver notes (what the helper was asked to do):")
        lines += [f"- {clean_text(note.body, 300)}" for note in notes[-12:]]
    if runs:
        lines.append("Recent runs of the browser helper:")
        for run in runs:
            alerts = f" (alerts: {', '.join(run.alerts[:3])})" if run.alerts else ""
            lines.append(f"- {run.site}: \"{clean_text(run.prompt, 120)}\" ended {run.status}{alerts}")
    return "\n".join(lines)


@dataclass
class Chat:
    pending: str = ""  # patient_id whose security question was just asked
    active: str = ""  # patient_id being discussed
    unlocked: dict[str, float] = field(default_factory=dict)  # patient_id -> until


class CheckIn:
    def __init__(self, records: Records | None, *, summarize: Summarize = llm_summary,
                 clock: Callable[[], float] = time.time) -> None:
        self.records = records
        self._summarize = summarize
        self._clock = clock
        self.chats: dict[str, Chat] = {}
        self._failures: dict[tuple[str, str], list[float]] = {}

    def end(self, sender: str) -> None:
        self.chats.pop(sender, None)

    def _chat(self, sender: str) -> Chat:
        if sender not in self.chats and len(self.chats) >= MAX_SESSIONS:
            for old in list(self.chats)[: MAX_SESSIONS // 2]:
                del self.chats[old]
        return self.chats.setdefault(sender, Chat())

    def _recent(self, key: tuple[str, str]) -> list[float]:
        now = self._clock()
        recent = [at for at in self._failures.get(key, []) if now - at < LOCK_SECONDS]
        self._failures[key] = recent
        return recent

    def _locked(self, sender: str, patient: Patient) -> bool:
        return (len(self._recent((sender, patient.patient_id))) >= MAX_TRIES
                or len(self._recent(("*", patient.patient_id))) >= MAX_PERSON_FAILURES)

    def _unlocked(self, chat: Chat, patient_id: str) -> bool:
        return chat.unlocked.get(patient_id, 0.0) > self._clock()

    async def reply(self, sender: str, text: str) -> str | None:
        """The reply to a caregiver's message, or None when it isn't about checking in on someone."""
        text = strip_mentions(clean_text(text, 1000)).strip()
        chat = self.chats.get(sender)
        if chat is not None and text.lower().strip(" .!") in RESET:
            self.end(sender)
            return "Okay."
        if self.records is None or await self.records.ready():
            return UNAVAILABLE if chat is not None or STATUS.search(text) else None
        try:
            patients = await self.records.patients()
        except RecordsError:
            return UNAVAILABLE
        by_id = {p.patient_id: p for p in patients}

        if chat is not None and chat.pending in by_id:
            return await self._answer(sender, chat, by_id[chat.pending], text)

        hits = find_patients(text, patients)
        if len(hits) > 1:
            return f"Which one: {' or '.join(p.full_name for p in hits)}?"
        target = hits[0] if hits else by_id.get(chat.active) if chat is not None else None
        if target is None:
            return "I don't know anyone by that name." if STATUS.search(text) else None
        chat = self._chat(sender)
        if not self._unlocked(chat, target.patient_id):
            return self._ask(sender, chat, target)

        chat.active = target.patient_id
        if THANKS.match(text):
            return "You're welcome."
        asks = bool(STATUS.search(text)) or "?" in text
        if NOTE.search(text) or (not hits and not asks and len(text.split()) >= 3):
            return await self._note(sender, target, text)
        return await self._report(target)

    def _ask(self, sender: str, chat: Chat, patient: Patient) -> str:
        if self._locked(sender, patient):
            return LOCKED
        if not has_answer(patient):
            return f"No security answer on file for {patient.full_name}."
        chat.pending = patient.patient_id
        return f"Answer this security question about {patient.first_name}: {patient.security_question}"

    async def _answer(self, sender: str, chat: Chat, patient: Patient, text: str) -> str:
        if self._locked(sender, patient):
            chat.pending = ""
            return LOCKED
        if is_correct(text, patient):
            chat.pending = ""
            chat.active = patient.patient_id
            chat.unlocked[patient.patient_id] = self._clock() + UNLOCK_SECONDS
            return await self._report(patient)
        now = self._clock()
        self._failures.setdefault((sender, patient.patient_id), []).append(now)
        self._failures.setdefault(("*", patient.patient_id), []).append(now)
        if self._locked(sender, patient):
            chat.pending = ""
            return LOCKED
        left = MAX_TRIES - len(self._recent((sender, patient.patient_id)))
        return f"That's not right. {left} {'try' if left == 1 else 'tries'} left."

    async def _report(self, patient: Patient) -> str:
        assert self.records is not None
        try:
            notes = await self.records.notes(patient.patient_id)
            runs = await self.records.runs(patient.full_name)
        except RecordsError:
            return UNAVAILABLE
        if not notes and not runs:
            return report(patient.first_name, [], [])
        try:
            data = await self._summarize(facts(patient, notes, runs))
            good, struggles = _items(data.get("good_with")), _items(data.get("struggles_with"))
        except llm.LLMError:
            return self._plain(patient, notes, runs)
        if not good and not struggles:
            return self._plain(patient, notes, runs)
        return report(patient.first_name, good, struggles)

    def _plain(self, patient: Patient, notes: list[Note], runs: list[Run]) -> str:
        good = list(dict.fromkeys(f"tasks on {run.site}" for run in runs if run.status == "done" and run.site))
        struggles = list(dict.fromkeys(alert for run in runs for alert in run.alerts if alert))
        lines = [report(patient.first_name, good[:MAX_ITEMS], struggles[:MAX_ITEMS])] if good or struggles else []
        if notes:
            lines += [f"Notes for {patient.first_name}:"] + [f"- {note.body}" for note in notes[-3:]]
        return "\n".join(lines)

    async def _note(self, sender: str, patient: Patient, text: str) -> str:
        assert self.records is not None
        author = "caregiver" if sender == "local" else f"caregiver {short_address(sender)}"
        try:
            await self.records.add_note(patient.patient_id, author, clean_text(text, 500))
        except RecordsError:
            return "Couldn't save that. Try again."
        return "Noted!"
