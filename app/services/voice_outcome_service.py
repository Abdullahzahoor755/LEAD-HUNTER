"""Voice call outcome classification with safe fallback behavior."""

from __future__ import annotations

from typing import Any, Dict

VALID_VOICE_OUTCOMES = {"interested", "not_interested", "callback", "no_answer", "voicemail", "unknown"}
DEFAULT_VOICE_SUMMARY = "Call ended but no reliable transcript was available."


class VoiceOutcomeService:
    async def classify(self, transcript: str) -> Dict[str, str]:
        clean_transcript = str(transcript or "").strip()
        if not clean_transcript:
            return {"outcome": "unknown", "summary": DEFAULT_VOICE_SUMMARY}
        try:
            return self._rule_based_classify(clean_transcript)
        except Exception:
            return {"outcome": "unknown", "summary": DEFAULT_VOICE_SUMMARY}

    def _rule_based_classify(self, transcript: str) -> Dict[str, str]:
        lower = transcript.lower()
        if any(token in lower for token in ("voicemail", "leave a message", "after the tone")):
            outcome = "voicemail"
        elif any(token in lower for token in ("no answer", "did not answer", "unanswered")):
            outcome = "no_answer"
        elif any(token in lower for token in ("call me back", "callback", "call back", "tomorrow", "next week")):
            outcome = "callback"
        elif any(token in lower for token in ("not interested", "stop calling", "remove me", "don't call")):
            outcome = "not_interested"
        elif any(token in lower for token in ("interested", "send me", "sounds good", "book", "schedule")):
            outcome = "interested"
        else:
            outcome = "unknown"
        return {"outcome": outcome, "summary": _safe_summary(transcript)}


def normalize_voice_outcome(result: Any) -> Dict[str, str]:
    if not isinstance(result, dict):
        return {"outcome": "unknown", "summary": DEFAULT_VOICE_SUMMARY}
    outcome = str(result.get("outcome") or "unknown").strip().lower()
    if outcome not in VALID_VOICE_OUTCOMES:
        outcome = "unknown"
    summary = str(result.get("summary") or "").strip()
    if not summary:
        summary = DEFAULT_VOICE_SUMMARY
    return {"outcome": outcome, "summary": summary[:1000]}


def _safe_summary(transcript: str) -> str:
    words = str(transcript or "").strip().split()
    if not words:
        return DEFAULT_VOICE_SUMMARY
    first_line = " ".join(words[:24])
    second_line = " ".join(words[24:48])
    return "\n".join(line for line in (first_line, second_line) if line)
