"""Persistent request reservations for a single-owner acceptance run."""
import json
from pathlib import Path


class CallBudget:
    def __init__(self, path: Path, limits: dict[str, int]):
        self.path = path
        if path.exists():
            self.state = json.loads(path.read_text(encoding="utf-8"))
            if self.state["limits"] != limits:
                raise RuntimeError("budget limits changed; use an explicitly new run")
        else:
            self.state = {"limits": limits, "used": dict.fromkeys(limits, 0)}
            self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def reserve(self, kind: str):
        if self.state["used"][kind] >= self.state["limits"][kind]:
            raise RuntimeError(f"{kind} budget exhausted")
        self.state["used"][kind] += 1
        self._save()
