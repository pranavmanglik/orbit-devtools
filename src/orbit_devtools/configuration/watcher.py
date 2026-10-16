# Copyright 2026-present Orbit Contributors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Cancellation-safe polling watcher for a validated TOML configuration section."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Generic, TypeVar

from orbit.config import (
    MAX_CONFIG_FILE_BYTES,
    MAX_CONFIG_PREFIX_LENGTH,
    Config,
    ConfigChange,
    load_config,
)
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
_SECTION_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,62}$")
_Fingerprint = tuple[int, int, int, str]
_MIN_INTERVAL_SECONDS = 0.05
_MAX_INTERVAL_SECONDS = 3_600.0


def _finite_number(value: object) -> bool:
    """Reject booleans, non-numbers, non-finite values, and integers too large for float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class ConfigWatcher(Generic[T]):
    """Watch one TOML section and apply only validated, observer-approved replacements.

    This optional development helper never changes the initial application composition. It
    observes the initial file for diagnostics, then submits later updates through ``Config``'s
    normal atomic reload and observer-veto path.
    """

    def __init__(
        self,
        config: Config,
        section: str,
        model: type[T],
        file: Path,
        *,
        interval: float = 1.0,
        values: Mapping[str, Any] | None = None,
        environment: Mapping[str, str] | None = None,
        prefix: str = "ORBIT_",
        max_file_bytes: int = 1024 * 1024,
    ) -> None:
        if not isinstance(config, Config):
            raise TypeError("ConfigWatcher requires a Config owner.")
        if not isinstance(section, str) or _SECTION_NAME.fullmatch(section) is None:
            raise ValueError("Configuration watcher sections must be lowercase identifiers.")
        if not _finite_number(interval) or not (
            _MIN_INTERVAL_SECONDS <= interval <= _MAX_INTERVAL_SECONDS
        ):
            raise ValueError("interval must be finite and between 0.05 and 3600 seconds.")
        if (
            isinstance(max_file_bytes, bool)
            or not isinstance(max_file_bytes, int)
            or not 1 <= max_file_bytes <= MAX_CONFIG_FILE_BYTES
        ):
            maximum_mib = MAX_CONFIG_FILE_BYTES // (1024 * 1024)
            raise ValueError(f"max_file_bytes must be between 1 and {maximum_mib} MiB.")
        if not isinstance(model, type) or not issubclass(model, BaseModel):
            raise TypeError("model must be a Pydantic model class.")
        if not isinstance(file, Path):
            raise TypeError("file must be a pathlib.Path.")
        if (
            not isinstance(prefix, str)
            or not 1 <= len(prefix) <= MAX_CONFIG_PREFIX_LENGTH
            or any(ord(character) < 32 or ord(character) == 127 for character in prefix)
        ):
            raise ValueError("prefix must be a bounded printable string.")
        self._config = config
        self._section = section
        self._model = model
        self._file = file
        self._interval = interval
        self._values = values
        self._environment = environment
        self._prefix = prefix
        self._max_file_bytes = max_file_bytes
        self._fingerprint: _Fingerprint | None = None
        self._task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._poll_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._last_error: Exception | None = None

    @property
    def running(self) -> bool:
        """Return whether this watcher currently owns a polling task."""
        return self._task is not None and not self._task.done()

    @property
    def last_error(self) -> Exception | None:
        """Return a generic failure marker without paths, values, or provider messages."""
        return self._last_error

    @staticmethod
    def _safe_error() -> RuntimeError:
        """Keep file paths, configuration values, and observer exceptions out of inspection."""
        return RuntimeError("Configuration watcher failed.")

    def _fingerprint_file(self) -> _Fingerprint:
        """Read a bounded digest so in-place edits with unchanged metadata are detected."""
        stat = self._file.stat()
        digest = hashlib.sha256()
        remaining = self._max_file_bytes + 1
        with self._file.open("rb") as stream:
            while remaining:
                chunk = stream.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
        return stat.st_mtime_ns, stat.st_size, stat.st_ino, digest.hexdigest()

    async def _load(self) -> T:
        """Run blocking file parsing away from the application's event loop."""
        return await asyncio.to_thread(
            load_config,
            self._model,
            file=self._file,
            values=self._values,
            environment=self._environment,
            prefix=self._prefix,
            max_file_bytes=self._max_file_bytes,
        )

    async def run_once(self) -> ConfigChange | None:
        """Poll once; serialize file observations and leave the initial config unchanged."""
        async with self._poll_lock:
            return await self._run_once()

    async def _run_once(self) -> ConfigChange | None:
        """Read, validate, and conditionally publish one stable file snapshot."""
        try:
            fingerprint = await asyncio.to_thread(self._fingerprint_file)
        except Exception:
            self._last_error = self._safe_error()
            return None
        if self._fingerprint == fingerprint:
            return None
        if self._fingerprint is None:
            # Validate the initial observation without changing the already-composed section.
            try:
                await self._load()
                if await asyncio.to_thread(self._fingerprint_file) != fingerprint:
                    self._last_error = RuntimeError("Configuration file changed during read.")
                else:
                    self._last_error = None
            except Exception:
                self._last_error = self._safe_error()
            self._fingerprint = fingerprint
            return None
        try:
            candidate = await self._load()
            if await asyncio.to_thread(self._fingerprint_file) != fingerprint:
                self._last_error = RuntimeError("Configuration file changed during read.")
                return None
            change = self._config.reload_section(self._section, candidate)
        except Exception:
            self._last_error = self._safe_error()
            return None
        self._fingerprint = fingerprint
        self._last_error = None
        return change

    async def start(self) -> None:
        """Start one polling task and join completed task cleanup before restarting."""
        async with self._lifecycle_lock:
            if self.running:
                raise RuntimeError("Configuration watcher is already running.")
            previous = self._task
            if previous is not None:
                await asyncio.gather(previous, return_exceptions=True)
                if self._task is previous:
                    self._task = None
            self._stop = asyncio.Event()
            task = asyncio.create_task(self._run(), name=f"orbit:config:{self._section}")
            self._task = task

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._last_error = self._safe_error()
                try:
                    async with asyncio.timeout(self._interval):
                        await self._stop.wait()
                except TimeoutError:
                    continue
        finally:
            if self._task is asyncio.current_task():
                self._task = None

    async def stop(self) -> None:
        """Stop polling and await cleanup even when the caller is cancelled."""
        async with self._lifecycle_lock:
            task = self._task
            if task is None:
                return
            self._stop.set()
            cancelled = False
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError


__all__ = ["ConfigWatcher"]
