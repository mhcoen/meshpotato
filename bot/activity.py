"""Bounded application facts about processing; message excerpts stay untrusted."""
from dataclasses import dataclass, field

ACTIVITY_BEGIN = "<<<BEGIN UNTRUSTED ACTIVITY MESSAGE EXCERPTS>>>"
ACTIVITY_END = "<<<END UNTRUSTED ACTIVITY MESSAGE EXCERPTS>>>"

REASONS = {
    "busy": "another request was ahead in the reply queue",
    "global": "the shared reply allowance was exhausted",
    "sender": "this sender's reply allowance was exhausted",
    "paused": "adaptive rate control paused replies",
    "queue-full": "the reply queue was full",
    "queue-expired": "the maximum queue wait expired",
    "paused-before-send": "the delivery deadline expired while waiting to send",
    "stale-sports-score": "the sports snapshot became too old to send",
    "repeat": "draft repeated an earlier reply",
    "parrot": "draft copied the incoming message",
    "personal-jab": "draft contained a personal jab",
    "radio-metaphor": "draft used an unrelated radio metaphor",
    "mention": "draft contained an unexpected mention",
    "direct-reply-limit": "the consecutive direct-reply limit was reached",
    "timeout": "the generation time budget expired",
    "empty reply": "the model returned an empty response",
    "model cooldown": "model requests are temporarily paused after repeated backend failures",
}


@dataclass
class Activity:
    identifier: int
    received_at: float
    received_time: str
    stage_at: float
    excerpt: str = ""
    stage: str = "screening"
    status: str = "processing"
    decision: str = ""
    reason: str = ""
    wait_reasons: list[str] = field(default_factory=list)
    durations: dict[str, float] = field(default_factory=dict)
    finished_at: float | None = None
    send_attempted: bool = False

    def move(self, stage: str, now: float) -> None:
        self.durations[self.stage] = self.durations.get(self.stage, 0.0) + max(0.0, now - self.stage_at)
        self.stage, self.stage_at = stage, now

    def waited_for(self, reason: str) -> None:
        description = REASONS.get(reason)
        if description and description not in self.wait_reasons:
            self.wait_reasons.append(description)

    def finish(self, status: str, reason: str, now: float) -> None:
        self.move(self.stage, now)
        self.status, self.reason, self.finished_at = status, reason, now

    def facts(self, now: float) -> dict:
        end = self.finished_at if self.finished_at is not None else now
        durations = dict(self.durations)
        if self.finished_at is None:
            durations[self.stage] = durations.get(self.stage, 0.0) + max(0.0, now - self.stage_at)
        return {
            "message_id": self.identifier,
            "received_seconds_ago": round(max(0.0, now - self.received_at), 3),
            "status": self.status if self.finished_at is not None else {
                "queue": "queued", "delivery_wait": "waiting for rate pause to end",
                "reply_hold": "waiting before sending", "sending": "radio command in progress",
            }.get(self.stage, "processing"),
            "decision": self.decision, "stage": self.stage, "reason": self.reason,
            "wait_reasons": list(self.wait_reasons),
            "elapsed_seconds": round(max(0.0, end - self.received_at), 3),
            "stage_seconds": {key: round(value, 3) for key, value in durations.items() if round(value, 3) > 0},
        }
