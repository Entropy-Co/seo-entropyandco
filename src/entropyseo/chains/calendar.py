"""SEO calendar queue stored in state/calendar.json.

The calendar is the single source of truth for which article runs next: strategy
appends rows, each run reserves exactly one queued row, and the graph writes the
outcome back to the same row. Writes are atomic (temp file + rename).

Row lifecycle: queued -> posting -> drafted | published | held | failed
A row stuck in `posting` (crashed run) returns to `queued` after STALE_POSTING_HOURS.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from entropyseo.config import get_settings
from entropyseo.schemas import CalendarCandidate

RowStatus = Literal["queued", "posting", "drafted", "published", "held", "failed", "skipped"]
STALE_POSTING_HOURS = 6


class CalendarRow(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    status: RowStatus = "queued"
    candidate: CalendarCandidate
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    reserved_at: str | None = None
    run_id: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)

    @property
    def title(self) -> str:
        return self.candidate.title


def _path() -> Path:
    return Path(get_settings().state_dir) / "calendar.json"


def read_rows() -> list[CalendarRow]:
    path = _path()
    if not path.exists():
        return []
    return [CalendarRow.model_validate(row) for row in json.loads(path.read_text(encoding="utf-8")).get("rows", [])]


def write_rows(rows: list[CalendarRow]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"rows": [r.model_dump(mode="json") for r in rows]}, indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".calendar-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)


def _update(row: CalendarRow) -> CalendarRow:
    rows = read_rows()
    write_rows([row if r.id == row.id else r for r in rows])
    return row


def release_stale_rows(now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    rows = read_rows()
    released = 0
    stale_after = timedelta(hours=STALE_POSTING_HOURS)
    for row in rows:
        if row.status == "posting" and row.reserved_at and now - datetime.fromisoformat(row.reserved_at) > stale_after:
            row.status, row.reserved_at, row.run_id = "queued", None, None
            released += 1
    if released:
        write_rows(rows)
    return released


def queued_rows() -> list[CalendarRow]:
    rows = [r for r in read_rows() if r.status == "queued"]
    return sorted(rows, key=lambda r: (-r.candidate.priority_score, r.created_at))


def all_titles() -> list[str]:
    """Titles of every non-failed row; strategy must not queue a topic the calendar already holds."""
    return [r.title for r in read_rows() if r.status not in {"failed", "skipped"}]


def append(candidate: CalendarCandidate) -> CalendarRow:
    rows = read_rows()
    row = CalendarRow(candidate=candidate)
    write_rows([*rows, row])
    return row


def reserve(row: CalendarRow, run_id: str) -> CalendarRow:
    row.status, row.run_id, row.reserved_at = "posting", run_id, datetime.now(UTC).isoformat()
    return _update(row)


def reserve_next(run_id: str) -> CalendarRow | None:
    release_stale_rows()
    queue = queued_rows()
    return reserve(queue[0], run_id) if queue else None


def finalize(row: CalendarRow, status: RowStatus, result: dict[str, Any]) -> CalendarRow:
    row.status, row.finished_at, row.result = status, datetime.now(UTC).isoformat(), result
    return _update(row)


def requeue(row_id: str) -> CalendarRow | None:
    rows = read_rows()
    for row in rows:
        if row.id == row_id:
            row.status, row.reserved_at, row.run_id, row.result = "queued", None, None, {}
            write_rows(rows)
            return row
    return None
