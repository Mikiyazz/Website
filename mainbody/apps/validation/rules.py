from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Finding:
    rule_code: str
    outcome: str  # pass / fail / manual
    location: str
    message: str


class ValidationRule(Protocol):
    code: str
    version: int

    def check(self, parsed_document: object) -> list[Finding]: ...


# No rules are silently installed. Unrecognized or unscanned files remain pending/manual.
RULES: dict[str, ValidationRule] = {}
