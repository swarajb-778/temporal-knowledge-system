"""Submission entry point: build() returns a KnowledgeSystem (see harness/contract.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:  # the harness puts its own contract module on sys.path; reuse its types when present
    import contract as _contract
except ImportError:  # running outside the harness: identical local copies of the contract types
    from tks import types as _contract

from tks.resolve import Knowledge  # noqa: E402


def build():
    return Knowledge(_contract)
