"""Strict payloads for persisted research commands, not arbitrary graph input."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from research_copilot.workspace import safe_component


class CommandPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StartPayload(CommandPayload):
    message: str = Field(min_length=1, max_length=8000)

    @field_validator("message")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("Message must not be blank")
        return value.strip()


class MessagePayload(StartPayload):
    message_id: str = Field(min_length=1, max_length=128)

    @field_validator("message_id")
    @classmethod
    def identity(cls, value):
        safe_component(value)
        return value


class ResumePayload(CommandPayload):
    decision: dict[str, dict] | None


class CorrectionPayload(CommandPayload):
    dispatch_call_id: str = Field(pattern=r"^wf-[a-f0-9]{32}$")


def parse_command(job):
    payload_type = {
        "start": StartPayload,
        "resume": ResumePayload,
        "message": MessagePayload,
        "correct": CorrectionPayload,
    }.get(job["kind"])
    if payload_type is None:
        raise ValueError("Unsupported research command")
    return payload_type.model_validate(job["payload"])
