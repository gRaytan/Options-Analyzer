"""Check results shared by every part of the risk gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class GateDecision:
    checks: Tuple[CheckResult, ...] = field(default_factory=tuple)

    @property
    def approved(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> List[CheckResult]:
        return [c for c in self.checks if not c.passed]

    def summary(self) -> str:
        if self.approved:
            return f"approved ({len(self.checks)} checks)"
        return "rejected: " + "; ".join(f"{c.name}: {c.detail}" for c in self.failures)

    def to_dict(self) -> dict:
        return {
            "approved": self.approved,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in self.checks
            ],
        }
