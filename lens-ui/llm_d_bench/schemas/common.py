"""Schemas shared by API domains."""

from datetime import datetime

from pydantic import BaseModel, Field


class PageInfo(BaseModel):
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    total: int = Field(ge=0)


class TaskRef(BaseModel):
    task_id: str
    status: str
    progress: float | None = Field(default=None, ge=0, le=1)
    message: str | None = None
    updated_at: datetime | None = None
