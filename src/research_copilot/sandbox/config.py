"""Server-owned execution limits. Model timeout is only allowed to narrow them."""

from pathlib import Path
import tomllib
from pydantic import BaseModel, ConfigDict, Field


class SandboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    image: str = "research-copilot-analysist:1"
    image_digest: str | None = None
    cpus: float = Field(default=1, gt=0, le=4)
    memory_mib: int = Field(default=1024, ge=128, le=4096)
    pids: int = Field(default=64, ge=8, le=256)
    timeout_seconds: int = Field(default=120, ge=1, le=600)
    temporary_mib: int = Field(default=256, ge=16, le=512)
    output_bytes: int = Field(default=32 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    output_files: int = Field(default=100, ge=1, le=1000)
    log_bytes: int = Field(default=64 * 1024, ge=1024, le=1024 * 1024)
    concurrency: int = Field(default=2, ge=1, le=2)
    executions_per_dispatch: int = Field(default=20, ge=1, le=20)
    queue_seconds: float = Field(default=30, gt=0, le=600)
    task_seconds: int = Field(default=1200, ge=1, le=3600)

    def effective_timeout(self, requested=None):
        if requested is not None and (isinstance(requested, bool) or requested < 0):
            raise ValueError("Invalid timeout")
        return min(requested or self.timeout_seconds, self.timeout_seconds)

    @classmethod
    def load(cls, path=Path("config/sandbox.toml")):
        return cls.model_validate(tomllib.loads(path.read_text("utf-8")))
