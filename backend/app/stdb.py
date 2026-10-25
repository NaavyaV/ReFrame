from typing import Any

import httpx

from .config import get_settings


class SpacetimeError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class SpacetimeClient:
    def __init__(self) -> None:
        s = get_settings()
        self._base = f"{s.spacetime_host}/v1/database/{s.spacetime_db}"
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {s.spacetime_token}"},
            timeout=15.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def sql(self, query: str) -> list[dict[str, Any]]:
        resp = await self._client.post(f"{self._base}/sql", content=query)
        if resp.status_code != 200:
            raise SpacetimeError(resp.text, resp.status_code)
        result = resp.json()[0]
        names = [e["name"]["some"] for e in result["schema"]["elements"]]
        return [dict(zip(names, row)) for row in result["rows"]]

    async def call(self, reducer: str, *args: Any) -> None:
        resp = await self._client.post(f"{self._base}/call/{reducer}", json=list(args))
        if resp.status_code != 200:
            raise SpacetimeError(resp.text, resp.status_code)

    async def find_user_by_email(self, email: str) -> dict[str, Any] | None:
        rows = await self.sql(f"SELECT * FROM app_user WHERE email = {_sql_literal(email)}")
        return rows[0] if rows else None

    async def find_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        rows = await self.sql(f"SELECT * FROM app_user WHERE user_id = {_sql_literal(user_id)}")
        return rows[0] if rows else None

    async def caretaker_notes(self, full_name: str) -> list[dict[str, Any]]:
        """Notes for the patient whose name matches, oldest first."""
        wanted = " ".join(full_name.split()).casefold()
        patients = await self.sql("SELECT * FROM patient")
        match = next((p for p in patients if " ".join(p["full_name"].split()).casefold() == wanted), None)
        if match is None:
            return []
        rows = await self.sql(f"SELECT * FROM caretaker_note WHERE patient_id = {_sql_literal(match['patient_id'])}")
        rows.sort(key=lambda r: r["note_id"])
        return [
            {"body": r["body"], "author": r["author"], "created_at": r["created_at"][0] // 1000}
            for r in rows
        ]
