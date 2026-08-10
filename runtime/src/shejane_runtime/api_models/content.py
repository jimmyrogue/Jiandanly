"""Workspace, Run input, presentation outline, and Artifact schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class PptxSlideOutline(BaseModel):
    index: int
    layout: str
    title: str
    bullets: list[str]
    notes: str
    shape_count: int
    image_count: int


class PptxOutlineResponse(BaseModel):
    slides: list[PptxSlideOutline]
    slide_count: int


class LocalWorkspaceAuthorization(BaseModel):
    id: str
    path: str
    label: str
    created_at: str
    last_used_at: str


class ListWorkspacesResponse(BaseModel):
    workspaces: list[LocalWorkspaceAuthorization]


class CreateWorkspaceRequest(BaseModel):
    path: str
    label: str = ""


class DiagnoseWorkspaceRequest(BaseModel):
    path: str


class LocalWorkspaceDiagnosis(BaseModel):
    """Result of POST /v1/workspaces/diagnose. The `reason` field
    drives the workspace-picker UI's "why is this disabled?" copy —
    keep the enum stable."""

    path: str
    exists: bool
    is_directory: bool
    authorized: bool
    reason: Literal["authorized", "not_authorized", "not_found", "not_directory"]
    workspace: LocalWorkspaceAuthorization | None = None


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Permissions (HITL)
# ---------------------------------------------------------------------------


class LocalArtifact(BaseModel):
    """Authorized Artifact metadata; blob bodies use the separate content route."""

    id: str
    title: str
    content: str
    content_type: str
    bytes: int
    sha256: str | None = None
    storage_kind: Literal["inline_text", "blob"]
    tool_name: str | None = None
    created_at: str


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
