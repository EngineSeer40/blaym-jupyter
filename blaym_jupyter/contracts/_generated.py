# GENERATED FILE - DO NOT EDIT.
# Source schema: blaym-event-envelope.schema.json (blaym-jupyter-types).
# Regenerate with: jlpm contracts:generate

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class BlaymEventCausalLangGraph(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    checkpointId: str | None = None
    node: str | None = None
    runId: str | None = None
    threadId: str


class BlaymEventCausal(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    cellId: str | None = None
    executionId: str | None = None
    langgraph: BlaymEventCausalLangGraph | None = None
    sourceRevision: float | None = None


class BlaymEventEnvelope(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    branchId: str
    causal: BlaymEventCausal | None = None
    eventId: UUID
    explorationId: str
    originId: str
    payload: dict[str, Any]
    schemaVersion: Annotated[int, Field(ge=1)]
    seq: Annotated[int, Field(ge=1)]
    type: str
    wallTimeMs: Annotated[int, Field(ge=0)]
