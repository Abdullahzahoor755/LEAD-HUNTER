"""Reusable AI skill prompt loading with a small safe allowlist."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar


class SkillPromptService:
    """Load product skill prompts without allowing arbitrary file access."""

    ALLOWED_SKILLS: ClassVar[set[str]] = {
        "campaign_generator",
        "agency_kit",
        "offer_matchmaker",
        "whatsapp_sales",
        "mini_agency",
    }
    _cache: ClassVar[dict[str, str]] = {}

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or Path(__file__).resolve().parents[1] / "skills"

    def load_skill(self, name: str) -> str:
        skill_name = str(name or "").strip().lower().replace("-", "_")
        if skill_name not in self.ALLOWED_SKILLS:
            raise ValueError(f"Unsupported AI skill: {name}")
        cached = self._cache.get(skill_name)
        if cached:
            return cached
        path = self.base_dir / f"{skill_name}.md"
        try:
            prompt = path.read_text(encoding="utf-8").strip()
        except OSError:
            prompt = self._fallback_prompt(skill_name)
        if not prompt:
            prompt = self._fallback_prompt(skill_name)
        self._cache[skill_name] = prompt
        return prompt

    def _fallback_prompt(self, skill_name: str) -> str:
        title = skill_name.replace("_", " ").title()
        return (
            f"You are the {title} skill for Lead Hunter AI. Improve the provided rule-based JSON, "
            "keep claims practical, preserve the existing JSON schema, and return valid JSON only."
        )
