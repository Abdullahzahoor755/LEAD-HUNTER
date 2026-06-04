"""Registry for discovering and invoking modular agents."""

from __future__ import annotations

from typing import Dict, Iterable

from app.agents.base import BaseAgent


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: Dict[str, BaseAgent] = {}

    def register(self, agent: BaseAgent) -> None:
        self._agents[agent.name] = agent

    def get(self, agent_name: str) -> BaseAgent:
        try:
            return self._agents[agent_name]
        except KeyError as error:
            raise KeyError(f"Agent '{agent_name}' is not registered.") from error

    def list_names(self) -> Iterable[str]:
        return sorted(self._agents)

