"""DTOs for the GPU driver (DRA / device plugin) installer API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

GpuAccessMode = Literal["dra", "plugin"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GpuDriverStatusResponse(StrictModel):
    access_mode: GpuAccessMode
    installed: bool
    ready: bool
    cluster_reachable: bool
    message: str


class GpuDriverInstallRequest(StrictModel):
    access_mode: GpuAccessMode = "dra"


class GpuDriverInstallResponse(StrictModel):
    access_mode: GpuAccessMode
    applied: bool
    message: str
