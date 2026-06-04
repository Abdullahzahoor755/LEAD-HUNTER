"""Worker bootstrap helpers."""

from __future__ import annotations

from app.agents.lead_generation import LeadGenerationAgent
from app.agents.outreach import OutreachAgent
from app.agents.followup import FollowupAgent
from app.agents.registry import AgentRegistry
from app.agents.reply_monitor import ReplyMonitorAgent
from app.db.session import DatabaseSession
from app.workers.jobs import AsyncJobQueue


def build_agent_registry() -> AgentRegistry:
    registry = AgentRegistry()
    registry.register(LeadGenerationAgent())
    registry.register(OutreachAgent())
    registry.register(FollowupAgent())
    registry.register(ReplyMonitorAgent())
    return registry


def build_job_queue(db: DatabaseSession | None) -> AsyncJobQueue:
    return AsyncJobQueue(db=db, agents=build_agent_registry())
