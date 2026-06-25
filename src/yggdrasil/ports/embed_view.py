"""EmbedView port."""
from __future__ import annotations
from typing import Protocol, runtime_checkable
from pydantic import BaseModel, ConfigDict
from yggdrasil.domain.models import Trajectory

class AspectTexts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_text: str
    scaffold_text: str

@runtime_checkable
class EmbedView(Protocol):
    @property
    def version(self) -> str: ...
    @property
    def domain(self) -> str: ...
    def build_aspect_texts(self, trajectory: Trajectory) -> AspectTexts: ...
    def build_query_aspect_texts(self, *, task: str | None, scaffold: str | None) -> AspectTexts: ...
