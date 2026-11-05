"""Field-for-field copies of the harness contract types, used when harness/contract.py is not importable."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Source:
    url: str
    snapshot_id: Optional[str] = None
    quote: Optional[str] = None


@dataclass
class Answer:
    text: str
    sources: list = field(default_factory=list)
    confident: bool = True
    conflict_note: Optional[str] = None


@dataclass
class Conflict:
    topic: str
    values: list
    sources: list
    resolution: Optional[str] = None
    reason: Optional[str] = None
