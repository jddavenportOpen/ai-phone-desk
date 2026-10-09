"""The terminal approver: for trying the desk locally, without a messaging vendor.

The bubble is printed; ``phone-desk approve <id>`` shows it again and asks the
person at the keyboard. It refuses to run unless stdin and stdout are both a
real terminal, so an agent piping "yes" into it gets nothing.

Honest limit: a TTY check proves someone is at a terminal, not WHO. An agent
that can drive a pseudo-terminal can pass it. Use it to test; use a messaging
approver (LoopMessage) for calls you care about.
"""
from __future__ import annotations

import sys
from typing import Callable, Optional

from ..brief import content_hash
from . import NotApproved


class TerminalApprover:
    name = "terminal"

    def __init__(self, out=None, input_fn: Optional[Callable[[str], str]] = None,
                 isatty: Optional[Callable[[], bool]] = None):
        self.out = out or sys.stdout
        self.input_fn = input_fn or input
        self.isatty = isatty or (lambda: sys.stdin.isatty() and sys.stdout.isatty())

    def send(self, text: str, ref: str) -> list[str]:
        print(f"\n{text}\n\n(terminal approver: run `phone-desk approve {ref}` at a real terminal to decide)",
              file=self.out)
        return [f"terminal:{ref}"]

    def notify(self, text: str) -> None:
        print(text, file=self.out)

    def ask(self, text: str, iid: str) -> Optional[str]:
        """'approve' | 'skip' | None (cancelled, nothing changes)."""
        if not self.isatty():
            raise NotApproved("the terminal approver only answers a real terminal (stdin and stdout are not a TTY)")
        print(f"\n{text}\n", file=self.out)
        ans = (self.input_fn(f"Type 👍 or 'yes {iid}' to place this call, 👎 or 'no' to drop it: ") or "").strip()
        if ans == "👍" or ans.lower() == f"yes {iid}":
            return "approve"
        if ans in ("👎",) or ans.lower() in ("no", f"no {iid}"):
            return "skip"
        return None

    def reprove(self, approval: dict, bubble_text: str, mids: list[str]) -> None:
        ev = approval.get("evidence") or {}
        if ev.get("kind") != "terminal" or ev.get("sha") != content_hash(bubble_text):
            raise NotApproved("no terminal approval on record for this exact bubble")
