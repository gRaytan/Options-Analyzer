"""Kill switch and persistent halts (plan T15, spec §6.4).

A halt has a scope:

* ``OPENING``: no new risk. Guards, rolls and closes keep running.
* ``ALL``: nothing is sent at all.

Ways to halt:

* **File:** ``touch data/trading/HALT`` (write ``all`` into it for a full halt).
* **Web UI:** the Halt button on the Bot Status page.
* **CLI:** ``python -m src.trading.kill_switch halt [--all] --reason "..."``
* **Automatic:** the engine calls :meth:`HaltManager.trigger`, e.g. on a
  reconciliation mismatch or an unhandled error.

Halts are journaled, so **a restart does not clear them**. Only a human
clears a halt (``resume`` from the UI or CLI, which also removes the HALT
file). Automatic code can escalate a halt but never lift one.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Callable, Optional

from src.trading.domain import HaltScope, HaltState, utcnow
from src.trading.journal import DEFAULT_JOURNAL_PATH, Journal

DEFAULT_HALT_FILE = Path("data/trading/HALT")
HALT_KEY = "halt"
SYSTEM = "system"


def _stricter(a: HaltState, b: HaltState) -> HaltState:
    if not a.active:
        return b
    if not b.active:
        return a
    return b if (b.scope is HaltScope.ALL and a.scope is not HaltScope.ALL) else a


def file_halt_state(path: Path | str) -> HaltState:
    """Halt state from the HALT file alone: present = halted; ``all`` inside = full halt."""
    path = Path(path)
    if not path.exists():
        return HaltState(False)
    try:
        content = path.read_text().strip().lower()
    except OSError:
        content = ""
    scope = HaltScope.ALL if content.startswith("all") else HaltScope.OPENING
    return HaltState(True, scope, f"HALT file present ({path})", None, "file")


class HaltManager:
    def __init__(
        self,
        journal: Journal,
        halt_file: Path | str = DEFAULT_HALT_FILE,
        clock: Callable = utcnow,
    ):
        self.journal = journal
        self.halt_file = Path(halt_file)
        self._clock = clock

    # -- state --------------------------------------------------------------

    def _journal_state(self) -> HaltState:
        record = self.journal.latest("halts", key=HALT_KEY)
        return record.data if record is not None else HaltState(False)

    def _file_state(self) -> HaltState:
        return file_halt_state(self.halt_file)

    def state(self) -> HaltState:
        """The effective halt: the stricter of the journal and the HALT file."""
        return _stricter(self._journal_state(), self._file_state())

    def allows_opening(self) -> bool:
        return not self.state().active

    def allows_closing(self) -> bool:
        st = self.state()
        return not (st.active and st.scope is HaltScope.ALL)

    # -- changes ------------------------------------------------------------

    def _record(self, state: HaltState) -> HaltState:
        self.journal.append("halts", state, key=HALT_KEY, ts=state.set_at)
        return state

    def halt(
        self, reason: str, scope: HaltScope = HaltScope.OPENING, by: str = "owner"
    ) -> HaltState:
        """Set a halt (human or automatic). Never weakens an existing halt."""
        current = self._journal_state()
        if current.active:
            if current.scope is HaltScope.ALL:
                scope = HaltScope.ALL
            reason = f"{current.reason}; {reason}"
        return self._record(HaltState(True, scope, reason, self._clock(), by))

    def trigger(self, reason: str, scope: HaltScope = HaltScope.OPENING) -> HaltState:
        """Automatic halt from the engine."""
        return self.halt(reason, scope, by=SYSTEM)

    def resume(self, by: str, note: str = "") -> HaltState:
        """Clear the halt. Humans only; the HALT file is removed too."""
        if not by or by == SYSTEM:
            raise PermissionError("only a human can clear a halt")
        try:
            self.halt_file.unlink()
        except FileNotFoundError:
            pass
        return self._record(
            HaltState(False, HaltScope.OPENING, note or "resumed", self._clock(), by)
        )


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Halt or resume the trading bot")
    sub = parser.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("halt", help="stop new trades (or everything with --all)")
    h.add_argument(
        "--all", action="store_true", help="stop everything, including closes"
    )
    h.add_argument("--reason", default="manual halt from CLI")
    r = sub.add_parser("resume", help="clear the halt")
    r.add_argument("--by", default=os.environ.get("USER", "owner"))
    r.add_argument("--note", default="")
    sub.add_parser("status", help="show the current halt state")
    args = parser.parse_args(argv)

    journal = Journal(os.environ.get("TRADING_JOURNAL", str(DEFAULT_JOURNAL_PATH)))
    manager = HaltManager(
        journal, os.environ.get("TRADING_HALT_FILE", str(DEFAULT_HALT_FILE))
    )
    if args.cmd == "halt":
        st = manager.halt(args.reason, HaltScope.ALL if args.all else HaltScope.OPENING)
    elif args.cmd == "resume":
        st = manager.resume(args.by, args.note)
        st = manager.state()
    else:
        st = manager.state()
    label = f"HALTED ({st.scope.value})" if st.active else "running (no halt)"
    print(f"{label}: {st.reason}" if st.reason else label)
    journal.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
