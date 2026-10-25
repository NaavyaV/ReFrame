"""SpacetimeDB access for the caretaker.

Same path as the backend: HTTP SQL to read, reducer calls to write.
Without SPACETIME_TOKEN it uses a fresh identity, which can read the public
caretaker tables and add notes but cannot seed patients.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:
    pass

DEFAULT_HOST = "https://maincloud.spacetimedb.com"
DEFAULT_DB = "reframe-g4w65"

PATIENTS = [
    (
        "maria-alvarez",
        "Maria Alvarez",
        "March 12, 1948",
        "San Antonio",
        "Mild hypertension",
        "Lisinopril 10mg each morning",
        "What city does Maria Alvarez live in?",
    ),
    (
        "james-okonkwo",
        "James Okonkwo",
        "November 2, 1952",
        "Plano",
        "Type 2 diabetes",
        "Metformin 500mg twice a day",
        "What year was James Okonkwo born?",
    ),
    (
        "helen-cho",
        "Helen Cho",
        "July 19, 1941",
        "Dallas",
        "Early arthritis in both knees",
        "Acetaminophen as needed",
        "What month was Helen Cho born?",
    ),
]


class SpacetimeError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _field(row: dict[str, Any], *names: str) -> str:
    for name in names:
        if name in row and row[name] is not None:
            return str(row[name])
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        if name.lower() in lowered and lowered[name.lower()] is not None:
            return str(lowered[name.lower()])
    return ""


class Spacetime:
    def __init__(self) -> None:
        host = (os.getenv("SPACETIME_HOST", "").strip() or DEFAULT_HOST).rstrip("/")
        name = os.getenv("SPACETIME_DB_NAME", "").strip() or DEFAULT_DB
        token = os.getenv("SPACETIME_TOKEN", "").strip()
        self.is_owner = bool(token)
        if not token:
            response = httpx.post(f"{host}/v1/identity", timeout=20.0)
            if response.status_code != 200:
                raise SpacetimeError(response.text, response.status_code)
            token = response.json()["token"]
        self._base = f"{host}/v1/database/{name}"
        self._headers = {"Authorization": f"Bearer {token}"}

    def sql(self, query: str) -> list[dict[str, Any]]:
        response = httpx.post(f"{self._base}/sql", content=query, headers=self._headers, timeout=20.0)
        if response.status_code != 200:
            raise SpacetimeError(response.text, response.status_code)
        result = response.json()[0]
        names = [element["name"]["some"] for element in result["schema"]["elements"]]
        return [dict(zip(names, row)) for row in result["rows"]]

    def call(self, reducer: str, *args: Any) -> None:
        response = httpx.post(
            f"{self._base}/call/{reducer}",
            json=list(args),
            headers=self._headers,
            timeout=20.0,
        )
        if response.status_code != 200:
            raise SpacetimeError(response.text, response.status_code)

    def ensure_patients(self) -> None:
        if not self.is_owner:
            return
        have = {_field(row, "patient_id", "patientId") for row in self.sql("SELECT * FROM patient")}
        for patient in PATIENTS:
            if patient[0] not in have:
                self.call("seed_patient", *patient)

    def patients(self) -> list[dict[str, str]]:
        rows = []
        for row in self.sql("SELECT * FROM patient"):
            rows.append(
                {
                    "patient_id": _field(row, "patient_id", "patientId"),
                    "full_name": _field(row, "full_name", "fullName"),
                    "date_of_birth": _field(row, "date_of_birth", "dateOfBirth"),
                    "city": _field(row, "city"),
                    "conditions": _field(row, "conditions"),
                    "medications": _field(row, "medications"),
                    "security_question": _field(row, "security_question", "securityQuestion"),
                }
            )
        return rows

    def notes(self, patient_id: str) -> list[dict[str, str]]:
        query = f"SELECT * FROM caretaker_note WHERE patient_id = {_sql_literal(patient_id)}"
        found = []
        for row in self.sql(query):
            found.append(
                {
                    "note_id": _field(row, "note_id", "noteId"),
                    "patient_id": _field(row, "patient_id", "patientId"),
                    "author": _field(row, "author"),
                    "body": _field(row, "body"),
                    "created_at": _field(row, "created_at", "createdAt"),
                }
            )
        found.sort(key=lambda note: int(note["note_id"] or 0))
        return found

    def add_note(self, patient_id: str, author: str, body: str) -> None:
        self.call("add_caretaker_note", patient_id, author, body)
