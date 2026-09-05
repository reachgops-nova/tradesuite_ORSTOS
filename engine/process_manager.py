"""
==========================================================
TradeSuite
engine/process_manager.py
==========================================================

Starts/stops/tracks the strategy engines from the GUI. Runs each engine
as an in-process background thread (not a separate subprocess/PID file --
the original scripts' pattern, which existed only because they were
launched independently by Windows Task Scheduler). The GUI owns each
thread handle directly, which is simpler and more reliable than the
original's PID-file/watchdog approach (and closes a real gap: the
original ORB scripts had no PID file or liveness check at all -- only
Tamil did).

Each engine also gets a small log queue so the GUI can show a live log
panel without engine code needing to know anything about PySide6.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytz

from engine.config_store import ConfigStore
from engine.orb_strategy import run_orb_day
from engine.tamil_strategy import run_tamil_day

IST = pytz.timezone("Asia/Kolkata")

STRATEGIES = {
    "orb": run_orb_day,
    "tamil": run_tamil_day,
}


def log_path_for(config: ConfigStore, strategy: str, day=None) -> Path:
    """Where a strategy's activity log for a given IST day lives. Shared
    with gui/activity_log_panel.py so the panel can show what happened
    before the app was last closed, instead of only what's arrived in
    the in-memory queue since launch."""
    day = day or datetime.now(IST).date()
    return config.logs_dir / f"{strategy}-{day}.log"


@dataclass
class StrategyHandle:
    name: str
    thread: threading.Thread
    stop_event: threading.Event
    # Set by the GUI to ask the engine to close everything NOW and keep
    # running. Deliberately routed through the engine rather than having
    # the GUI place its own SELL orders: the engine owns the in-memory
    # position state, so a GUI-side exit would race its monitor and could
    # double-sell the same position.
    manual_exit_event: threading.Event = field(default_factory=threading.Event)
    log_queue: "queue.Queue[str]" = field(default_factory=queue.Queue)

    def is_running(self) -> bool:
        return self.thread.is_alive()

    def stop(self, join_timeout: float = 5.0) -> None:
        self.stop_event.set()
        self.thread.join(timeout=join_timeout)

    def drain_logs(self) -> list[str]:
        lines = []
        while True:
            try:
                lines.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        return lines


class ProcessManager:
    """One instance owned by the GUI's main window for the app's lifetime."""

    def __init__(self, config: ConfigStore):
        self.config = config
        self._handles: dict[str, StrategyHandle] = {}

    def is_running(self, strategy: str) -> bool:
        handle = self._handles.get(strategy)
        return handle is not None and handle.is_running()

    def start(self, strategy: str) -> StrategyHandle:
        if strategy not in STRATEGIES:
            raise ValueError(f"Unknown strategy: {strategy}")
        if self.is_running(strategy):
            return self._handles[strategy]

        stop_event = threading.Event()
        manual_exit_event = threading.Event()
        log_queue: "queue.Queue[str]" = queue.Queue()

        def log_fn(line: str) -> None:
            log_queue.put(line)
            # Also persisted, so the activity log survives an app restart and
            # is there to look at after the fact ("what did it see at 09:26?").
            # Path resolved per line rather than once at start, so a run left
            # going across IST midnight rolls into the new day's file.
            try:
                with open(log_path_for(self.config, strategy), "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass  # never let a logging problem take a running strategy down

        run_fn = STRATEGIES[strategy]
        thread = threading.Thread(
            target=run_fn, args=(self.config,),
            kwargs={"log_fn": log_fn, "stop_event": stop_event, "manual_exit_event": manual_exit_event},
            name=f"tradesuite-{strategy}", daemon=True,
        )
        handle = StrategyHandle(name=strategy, thread=thread, stop_event=stop_event,
                                 manual_exit_event=manual_exit_event, log_queue=log_queue)
        self._handles[strategy] = handle
        thread.start()
        return handle

    def stop(self, strategy: str) -> None:
        handle = self._handles.get(strategy)
        if handle is not None:
            handle.stop()

    def request_manual_exit(self, strategy: str) -> bool:
        """Ask a running strategy to close all its open positions now.
        Returns False if it isn't running."""
        handle = self._handles.get(strategy)
        if handle is None or not handle.is_running():
            return False
        handle.manual_exit_event.set()
        return True

    def stop_all(self) -> None:
        for name in list(self._handles):
            self.stop(name)

    def handle(self, strategy: str) -> StrategyHandle | None:
        return self._handles.get(strategy)
