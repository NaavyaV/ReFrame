"""Patient records and caretaker notes, for a caregiver checking in on someone.

SpacetimeRecords reads the patient and caretaker_note tables, adds notes with the
add_caretaker_note reducer, and reads the tracker's agent_run and agent_alert rows
for the person's name when they exist. MemoryRecords is the same thing in memory,
for tests.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from shared.spacetime import Spacetime, SpacetimeError, check_tables, field as column, sql_literal

TABLES = ["patient", "caretaker_note"]
RECHECK_SECONDS = 300
MAX_RUNS = 15


class RecordsError(Exception):
    pass


@dataclass
class Patient:
    patient_id: str
    full_name: str
    date_of_birth: str = ""
    city: str = ""
    conditions: str = ""
    medications: str = ""
    security_question: str = ""
    answer_hash: str = ""  # from the private patient_secret table; see checkin.answer_hash

    @property
    def first_name(self) -> str:
        return self.full_name.split()[0] if self.full_name.strip() else self.patient_id


@dataclass
class Note:
    author: str
    body: str


@dataclass
class Run:
    """One tracked run of the browser extension."""
    site: str
    prompt: str
    status: str
    alerts: list[str] = field(default_factory=list)


class Records:
    async def ready(self) -> str | None:
        """None when usable, otherwise why not."""
        raise NotImplementedError

    async def patients(self) -> list[Patient]:
        raise NotImplementedError

    async def notes(self, patient_id: str) -> list[Note]:
        """Oldest first."""
        raise NotImplementedError

    async def runs(self, full_name: str) -> list[Run]:
        """Newest first. Empty when the tracker has nothing for that name."""
        raise NotImplementedError

    async def add_note(self, patient_id: str, author: str, body: str) -> None:
        raise NotImplementedError


class MemoryRecords(Records):
    def __init__(self, patients: list[Patient] | None = None) -> None:
        self.people = {p.patient_id: p for p in patients or []}
        self.saved: dict[str, list[Note]] = {}
        self.tracked: dict[str, list[Run]] = {}

    async def ready(self) -> str | None:
        return None

    async def patients(self) -> list[Patient]:
        return list(self.people.values())

    async def notes(self, patient_id: str) -> list[Note]:
        return list(self.saved.get(patient_id, []))

    async def runs(self, full_name: str) -> list[Run]:
        return list(self.tracked.get(full_name, []))

    async def add_note(self, patient_id: str, author: str, body: str) -> None:
        if patient_id not in self.people:
            raise RecordsError("not_found")
        self.saved.setdefault(patient_id, []).append(Note(author, body))


def _text(row: dict[str, Any], *names: str) -> str:
    return str(column(row, *names) or "")


def _micros(value: Any) -> int:
    if isinstance(value, list):
        value = value[0] if value else 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class SpacetimeRecords(Records):
    def __init__(self, db: Spacetime | None = None) -> None:
        self.db = db
        self._reason: str | None = "not checked yet"
        self._checked = -RECHECK_SECONDS * 2.0

    async def ready(self) -> str | None:
        now = time.monotonic()
        if self._reason is None or now - self._checked < RECHECK_SECONDS:
            return self._reason
        self._checked = now
        if self.db is None:
            self.db, status = await check_tables(TABLES)
        else:
            status = await self.db.preflight(TABLES)
        problems = [f"{table}: {state}" for table, state in status.items() if state != "ok"]
        self._reason = "; ".join(problems) or None
        return self._reason

    async def _sql(self, query: str) -> list[dict[str, Any]]:
        if self.db is None:
            raise RecordsError("SpacetimeDB isn't configured.")
        try:
            return await self.db.sql(query)
        except SpacetimeError as exc:
            raise RecordsError(str(exc)) from exc

    async def patients(self) -> list[Patient]:
        try:
            secrets = {_text(row, "patient_id", "patientId"): _text(row, "answer_hash", "answerHash")
                       for row in await self._sql("SELECT * FROM patient_secret")}
        except RecordsError:
            secrets = {}
        return [
            Patient(
                patient_id=_text(row, "patient_id", "patientId"),
                full_name=_text(row, "full_name", "fullName"),
                date_of_birth=_text(row, "date_of_birth", "dateOfBirth"),
                city=_text(row, "city"),
                conditions=_text(row, "conditions"),
                medications=_text(row, "medications"),
                security_question=_text(row, "security_question", "securityQuestion"),
                answer_hash=secrets.get(_text(row, "patient_id", "patientId"), ""),
            )
            for row in await self._sql("SELECT * FROM patient")
        ]

    async def notes(self, patient_id: str) -> list[Note]:
        rows = await self._sql(f"SELECT * FROM caretaker_note WHERE patient_id = {sql_literal(patient_id)}")
        rows.sort(key=lambda row: _micros(column(row, "note_id", "noteId")))
        return [Note(_text(row, "author"), _text(row, "body")) for row in rows]

    async def runs(self, full_name: str) -> list[Run]:
        try:
            runs = await self._sql(f"SELECT * FROM agent_run WHERE user_name = {sql_literal(full_name)}")
            runs.sort(key=lambda row: _micros(column(row, "started_at", "startedAt")), reverse=True)
            runs = runs[:MAX_RUNS]
            alerts: dict[str, list[str]] = {}
            for user_id in {_text(row, "user_id", "userId") for row in runs}:
                for alert in await self._sql(f"SELECT * FROM agent_alert WHERE user_id = {sql_literal(user_id)}"):
                    alerts.setdefault(_text(alert, "run_id", "runId"), []).append(_text(alert, "title"))
        except RecordsError:
            return []
        return [
            Run(_text(row, "site"), _text(row, "prompt"), _text(row, "status"),
                alerts.get(_text(row, "run_id", "runId"), []))
            for row in runs
        ]

    async def add_note(self, patient_id: str, author: str, body: str) -> None:
        if self.db is None:
            raise RecordsError("SpacetimeDB isn't configured.")
        try:
            await self.db.call("add_caretaker_note", patient_id, author, body)
        except SpacetimeError as exc:
            raise RecordsError(str(exc)) from exc
