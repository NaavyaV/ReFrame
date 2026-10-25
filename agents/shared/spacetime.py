"""SpacetimeDB access for the agents.

Same path as the backend: HTTP SQL to read, reducer calls to write, with
SPACETIME_TOKEN (the module owner's identity) as the bearer token. preflight()
reports which tables exist so an agent can switch a feature off with a clear
message instead of failing on every request. Agents never publish or migrate the
database; see the README for the deployment steps.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

DEFAULT_HOST = "https://maincloud.spacetimedb.com"
MIGRATION_HINT = (
    "Publish the updated module in spacetime/spacetimedb from the owner identity "
    "(see README, 'Database tables for the agents'). Agents never publish it themselves."
)
_MISSING = re.compile(r"no such table:\s*`?([A-Za-z0-9_]+)`?", re.I)


class SpacetimeError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SpacetimeNotConfigured(SpacetimeError):
    pass


class MissingTable(SpacetimeError):
    def __init__(self, table: str, database: str) -> None:
        super().__init__(
            f"SpacetimeDB database '{database}' has no table '{table}' (or it is private to another "
            f"identity). {MIGRATION_HINT}",
            400,
        )
        self.table = table


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def field(row: dict[str, Any], *names: str) -> Any:
    """A column by its snake_case or camelCase name."""
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value is not None:
            return value
    return None


class Spacetime:
    def __init__(
        self,
        host: str,
        database: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.database = database
        self._base = f"{host.rstrip('/')}/v1/database/{database}"
        self._token = token
        self._transport = transport
        self._timeout = timeout

    @classmethod
    def from_env(cls) -> "Spacetime":
        host = os.getenv("SPACETIME_HOST", "").strip() or DEFAULT_HOST
        database = os.getenv("SPACETIME_DB_NAME", "").strip()
        token = os.getenv("SPACETIME_TOKEN", "").strip()
        if not database or not token:
            raise SpacetimeNotConfigured("SPACETIME_DB_NAME and SPACETIME_TOKEN must both be set in .env.")
        return cls(host, database, token)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=self._timeout,
            transport=self._transport,
        )

    def _error(self, response: httpx.Response) -> SpacetimeError:
        text = response.text[:300].replace(self._token, "***")
        missing = _MISSING.search(text)
        if missing:
            return MissingTable(missing.group(1), self.database)
        if response.status_code in (401, 403):
            return SpacetimeError(
                f"SpacetimeDB refused SPACETIME_TOKEN for '{self.database}' ({response.status_code}). "
                "Use the token of the identity that published the module.",
                response.status_code,
            )
        return SpacetimeError(f"SpacetimeDB error {response.status_code}: {text}", response.status_code)

    async def sql(self, query: str) -> list[dict[str, Any]]:
        try:
            async with self._client() as client:
                response = await client.post(f"{self._base}/sql", content=query)
        except httpx.HTTPError as exc:
            raise SpacetimeError(f"Couldn't reach SpacetimeDB ({exc.__class__.__name__}).") from exc
        if response.status_code != 200:
            raise self._error(response)
        result = response.json()[0]
        names = [element["name"]["some"] for element in result["schema"]["elements"]]
        return [dict(zip(names, row)) for row in result["rows"]]

    async def call(self, reducer: str, *args: Any) -> None:
        try:
            async with self._client() as client:
                response = await client.post(f"{self._base}/call/{reducer}", json=list(args))
        except httpx.HTTPError as exc:
            raise SpacetimeError(f"Couldn't reach SpacetimeDB ({exc.__class__.__name__}).") from exc
        if response.status_code != 200:
            error = self._error(response)
            if "no such reducer" in str(error).lower() or response.status_code == 404:
                raise SpacetimeError(f"SpacetimeDB has no reducer '{reducer}'. {MIGRATION_HINT}", 404)
            raise error

    async def preflight(self, tables: list[str]) -> dict[str, str]:
        """For each table: "ok", or why it can't be used."""
        status: dict[str, str] = {}
        for table in tables:
            try:
                await self.sql(f"SELECT COUNT(*) AS n FROM {table}")
                status[table] = "ok"
            except MissingTable:
                status[table] = "missing"
            except SpacetimeError as exc:
                status[table] = str(exc)
        return status


async def check_tables(tables: list[str]) -> tuple[Spacetime | None, dict[str, str]]:
    """The client and each table's status, or (None, reasons) when SpacetimeDB isn't configured."""
    try:
        db = Spacetime.from_env()
    except SpacetimeNotConfigured as exc:
        return None, {table: str(exc) for table in tables}
    return db, await db.preflight(tables)
