"""File-based state: one JSON document per concern, written atomically under a lock.

    desk.json      call requests and their lifecycle
    tokens.json    approval tokens (see gate.py)
    ledger.jsonl   append-only audit of every gate decision
    consumed.json  approval events already spent (a 👍 approves once)
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import time
from pathlib import Path


class StateBusy(RuntimeError):
    pass


class Store:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._depth = 0

    def path(self, name: str) -> Path:
        return self.dir / name

    @contextlib.contextmanager
    def locked(self, timeout: float = 30.0):
        """Exclusive across processes; re-entrant within this Store object."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        lock = self.path(".lock")
        with open(lock, "a+") as fh:
            deadline = time.time() + timeout
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.time() >= deadline:
                        raise StateBusy("phone desk state is busy; try again in a moment") from None
                    time.sleep(0.1)
            self._depth = 1
            try:
                yield
            finally:
                self._depth = 0
                fcntl.flock(fh, fcntl.LOCK_UN)

    def read(self, name: str, default):
        try:
            return json.loads(self.path(name).read_text())
        except FileNotFoundError:
            return default
        except ValueError as exc:
            # A corrupt file is a failure, never an empty one: an empty token or
            # consumed list would let an old approval count again.
            raise RuntimeError(f"{self.path(name)} is unreadable ({exc}); fix or remove it by hand") from None

    def write(self, name: str, data) -> None:
        p = self.path(name)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, default=str, ensure_ascii=False))
        tmp.replace(p)

    def append(self, name: str, row: dict) -> None:
        with open(self.path(name), "a") as fh:
            fh.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")

    def lines(self, name: str) -> list[dict]:
        try:
            raw = self.path(name).read_text().splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in raw:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    # desk.json
    def load_desk(self) -> dict:
        data = self.read("desk.json", {})
        data.setdefault("items", {})
        data.setdefault("external", {"seen": [], "accepted": {}})
        return data

    def save_desk(self, data: dict) -> None:
        self.write("desk.json", data)
