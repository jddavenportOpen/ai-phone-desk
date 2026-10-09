"""Approvers put a bubble in front of the owner and prove the owner's answer.

An approver has three jobs:
  send(text, ref)            deliver the bubble; return the message id(s)
  notify(text)               tell the owner how something went (no approval meaning)
  reprove(approval, text, mids)
                             at dial time, re-check the recorded approval against
                             the source of truth; raise NotApproved if it no
                             longer holds

Two ship here: ``terminal`` (stdin, for local testing) and ``loopmessage``
(the owner's 👍 tapback in iMessage, proven against LoopMessage's records).
"""
from __future__ import annotations

from typing import Protocol


class NotApproved(RuntimeError):
    """No fresh, unused, provable 👍 from the owner on this exact bubble."""


class AlreadyUsed(NotApproved):
    pass


class Approver(Protocol):
    name: str

    def send(self, text: str, ref: str) -> list[str]: ...

    def notify(self, text: str) -> None: ...

    def reprove(self, approval: dict, bubble_text: str, mids: list[str]) -> None: ...


def build(cfg) -> Approver:
    if cfg.approver == "loopmessage":
        from .loopmessage import LoopMessageApprover
        return LoopMessageApprover(cfg)
    from .terminal import TerminalApprover
    return TerminalApprover()
