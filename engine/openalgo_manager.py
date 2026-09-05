"""
==========================================================
TradeSuite
engine/openalgo_manager.py
==========================================================

GUI-friendly wrapper around installer/openalgo_bootstrap.py's plain
functions -- same background-thread-plus-log-queue shape as
process_manager.py's StrategyHandle, so the GUI doesn't freeze during the
(potentially minutes-long, first-run-only) clone/pip-install, and can
show progress instead of just hanging.
"""

from __future__ import annotations

import queue
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse

from engine.config_store import ConfigStore
from installer import openalgo_bootstrap as boot


class OpenAlgoManager:
    def __init__(self, config: ConfigStore):
        self.config = config
        self.runtime_dir = config.install_dir / "openalgo_runtime"
        self._bootstrap_thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self.log_queue: "queue.Queue[str]" = queue.Queue()

    def is_bootstrapped(self) -> bool:
        return boot.is_bootstrapped(self.runtime_dir)

    def is_bootstrapping(self) -> bool:
        return self._bootstrap_thread is not None and self._bootstrap_thread.is_alive()

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start_bootstrap_async(self) -> None:
        if self.is_bootstrapping() or self.is_bootstrapped():
            return
        if not self.config.is_broker_credentials_set():
            # Not just a nicety -- OpenAlgo hard-refuses to even start
            # without a validly-formatted broker key already in .env (see
            # openalgo_bootstrap.py's notes), so there's no point
            # bootstrapping without these yet.
            self.log_queue.put("ERROR: Enter your broker credentials first (see the Broker Credentials step above).")
            return

        def _log(msg: str) -> None:
            self.log_queue.put(msg)

        def _work() -> None:
            host_ip = urlparse(self.config.openalgo_host).hostname or "127.0.0.1"
            port = urlparse(self.config.openalgo_host).port or 5001
            ok, msg = boot.bootstrap(
                self.runtime_dir, host_ip, port, log_fn=_log,
                broker=self.config.broker_name,
                broker_api_key=self.config.broker_combined_api_key,
                broker_api_secret=self.config.broker_api_secret,
            )
            _log(msg if ok else f"ERROR: {msg}")

        self._bootstrap_thread = threading.Thread(target=_work, name="tradesuite-openalgo-bootstrap", daemon=True)
        self._bootstrap_thread.start()

    def start_process(self) -> None:
        if self.is_running():
            return
        if not self.is_bootstrapped():
            raise RuntimeError("OpenAlgo isn't set up yet -- run the bootstrap first.")
        # Re-sync .env with current config before every launch, not just at
        # first bootstrap -- otherwise updating credentials in Step 1 after
        # the initial setup would silently have no effect, since .env only
        # ever got written once. Cheap and idempotent (see configure_env's
        # own regex replace-in-place logic), safe to call on every start.
        host_ip = urlparse(self.config.openalgo_host).hostname or "127.0.0.1"
        port = urlparse(self.config.openalgo_host).port or 5001
        boot.configure_env(
            self.runtime_dir, host_ip, port, log_fn=self.log_queue.put,
            broker=self.config.broker_name,
            broker_api_key=self.config.broker_combined_api_key,
            broker_api_secret=self.config.broker_api_secret,
        )
        self._process = boot.launch(self.runtime_dir)

    def stop_process(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None

    def drain_logs(self) -> list[str]:
        lines = []
        while True:
            try:
                lines.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        return lines
