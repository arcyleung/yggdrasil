"""coding_v1 EmbedView templates."""
from __future__ import annotations

from yggdrasil.domain.models import Trajectory
from yggdrasil.ports.embed_view import AspectTexts, EmbedView


class CodingEmbedViewV1:
    """Deterministic task/scaffold embed templates for coding domain."""

    VERSION = "coding_v1"
    DOMAIN = "coding"

    @property
    def version(self) -> str:
        return self.VERSION

    @property
    def domain(self) -> str:
        return self.DOMAIN

    def build_aspect_texts(self, trajectory: Trajectory) -> AspectTexts:
        return AspectTexts(
            task_text=self._build_task_text(
                task=trajectory.task_text,
                tags=trajectory.tags,
            ),
            scaffold_text=self._build_scaffold_text(
                scaffold=trajectory.scaffold_text,
                phase=trajectory.progress.phase if trajectory.progress else None,
                progress_summary=trajectory.progress.summary if trajectory.progress else None,
            ),
        )

    def build_query_aspect_texts(
        self,
        *,
        task: str | None,
        scaffold: str | None,
        include_attempt_history: bool = False,
    ) -> AspectTexts:
        if include_attempt_history:
            raise ValueError("include_attempt_history is not supported in PoC coding_v1")
        task_text = ""
        scaffold_text = ""
        if task is not None and task.strip():
            task_text = self._build_task_text(task=task, tags=None)
        if scaffold is not None and scaffold.strip():
            scaffold_text = self._build_scaffold_query_text(scaffold=scaffold)
        return AspectTexts(task_text=task_text, scaffold_text=scaffold_text)

    def _build_task_text(self, *, task: str, tags: list[str] | None) -> str:
        lines = [
            f"domain: {self.DOMAIN}",
            f"task: {task.strip()}",
        ]
        if tags:
            sorted_tags = ", ".join(sorted(t.strip() for t in tags if t and t.strip()))
            if sorted_tags:
                lines.append(f"tags: {sorted_tags}")
        return "\n".join(lines)

    def _build_scaffold_text(
        self,
        *,
        scaffold: str,
        phase: str | None,
        progress_summary: str | None,
    ) -> str:
        lines = [
            f"domain: {self.DOMAIN}",
            f"scaffold: {scaffold.strip()}",
        ]
        if phase and phase.strip():
            lines.append(f"progress_phase: {phase.strip()}")
        if progress_summary and progress_summary.strip():
            lines.append(f"progress_summary: {progress_summary.strip()}")
        return "\n".join(lines)

    def _build_scaffold_query_text(self, *, scaffold: str) -> str:
        return "\n".join(
            [
                f"domain: {self.DOMAIN}",
                f"scaffold: {scaffold.strip()}",
            ]
        )


_: type[EmbedView] = CodingEmbedViewV1  # type: ignore[misc,assignment]
