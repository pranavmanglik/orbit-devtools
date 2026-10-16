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
"""Reload, lifecycle, input-boundary, and privacy behavior for the devtools watcher."""

import asyncio
import os
import time
from pathlib import Path

import pytest
from orbit import Application, ApplicationConfig
from orbit.config import Config
from pydantic import BaseModel, ConfigDict, SecretStr

from orbit_devtools import ConfigWatcher
from orbit_devtools.configuration import watcher as watcher_module


class Database(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str
    password: SecretStr


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: Database


def make_config() -> Config:
    config = Config(ApplicationConfig(name="watcher-test"))
    config.register("database", Settings(database=Database(host="db-a", password="secret")))
    config.freeze()
    return config


def write_settings(file: Path, host: str = "db-a") -> None:
    file.write_text(f'[database]\nhost="{host}"\npassword="secret"\n')


@pytest.mark.asyncio
async def test_applies_valid_updates_and_retains_last_good_config(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    write_settings(file)
    config = make_config()
    watcher = ConfigWatcher(config, "database", Settings, file)

    assert await watcher.run_once() is None
    write_settings(file, "db-b")
    change = await watcher.run_once()
    assert change is not None
    assert config.get("database").database.host == "db-b"

    file.write_text("[database]\nhost=\n")
    assert await watcher.run_once() is None
    assert watcher.last_error is not None
    assert config.get("database").database.host == "db-b"


@pytest.mark.asyncio
async def test_invalid_initial_observation_does_not_mutate_config(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    file.write_text("[database]\nhost=\n")
    config = make_config()
    watcher = ConfigWatcher(config, "database", Settings, file)

    assert await watcher.run_once() is None
    assert watcher.last_error is not None
    assert "ConfigurationError" not in str(watcher.last_error)
    assert config.get("database").database.host == "db-a"


@pytest.mark.asyncio
async def test_content_digest_detects_same_size_same_mtime_edits(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    write_settings(file)
    config = make_config()
    watcher = ConfigWatcher(config, "database", Settings, file)
    assert await watcher.run_once() is None

    original_stat = file.stat()
    write_settings(file, "db-b")
    os.utime(file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert file.stat().st_size == original_stat.st_size
    assert file.stat().st_mtime_ns == original_stat.st_mtime_ns

    assert await watcher.run_once() is not None
    assert config.get("database").database.host == "db-b"


@pytest.mark.asyncio
async def test_atomic_file_replacement_is_detected(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    candidate = tmp_path / "candidate.toml"
    write_settings(file)
    write_settings(candidate, "db-b")
    config = make_config()
    watcher = ConfigWatcher(config, "database", Settings, file)
    assert await watcher.run_once() is None

    original_stat = file.stat()
    os.utime(candidate, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    os.replace(candidate, file)

    assert await watcher.run_once() is not None
    assert config.get("database").database.host == "db-b"


@pytest.mark.asyncio
async def test_file_change_during_parse_is_not_applied(tmp_path: Path, monkeypatch) -> None:
    file = tmp_path / "config.toml"
    write_settings(file)
    config = make_config()
    watcher = ConfigWatcher(config, "database", Settings, file)
    assert await watcher.run_once() is None
    write_settings(file, "db-c")
    original_load = watcher_module.load_config

    def replace_after_load(*args, **kwargs):
        candidate = original_load(*args, **kwargs)
        write_settings(file, "db-b")
        return candidate

    monkeypatch.setattr(watcher_module, "load_config", replace_after_load)
    assert await watcher.run_once() is None
    assert config.get("database").database.host == "db-a"
    assert watcher.last_error is not None

    monkeypatch.setattr(watcher_module, "load_config", original_load)
    assert await watcher.run_once() is not None
    assert config.get("database").database.host == "db-b"


@pytest.mark.asyncio
async def test_observer_failure_is_sanitized(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    write_settings(file)
    config = make_config()

    def reject(change):
        raise RuntimeError("secret=not-for-inspection")

    config.subscribe(reject)
    watcher = ConfigWatcher(config, "database", Settings, file)
    assert await watcher.run_once() is None
    write_settings(file, "db-b")
    assert await watcher.run_once() is None
    assert str(watcher.last_error) == "Configuration watcher failed."
    assert config.get("database").database.host == "db-a"


@pytest.mark.asyncio
async def test_file_io_runs_off_the_event_loop(tmp_path: Path, monkeypatch) -> None:
    file = tmp_path / "config.toml"
    write_settings(file)
    config = make_config()
    watcher = ConfigWatcher(config, "database", Settings, file)
    original_load = watcher_module.load_config

    def slow_load(*args, **kwargs):
        time.sleep(0.03)
        return original_load(*args, **kwargs)

    monkeypatch.setattr(watcher_module, "load_config", slow_load)
    heartbeat = asyncio.create_task(asyncio.sleep(0.001))
    await watcher.run_once()
    assert heartbeat.done()


@pytest.mark.asyncio
async def test_start_stop_and_restart_own_one_polling_task(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    write_settings(file)
    watcher = ConfigWatcher(make_config(), "database", Settings, file, interval=0.05)
    await watcher.start()
    with pytest.raises(RuntimeError, match="already running"):
        await watcher.start()
    await watcher.stop()
    await watcher.stop()

    await watcher.start()
    previous = watcher._task
    assert previous is not None
    previous.cancel()
    await asyncio.gather(previous, return_exceptions=True)
    results = await asyncio.gather(watcher.start(), watcher.start(), return_exceptions=True)
    assert sorted(type(result).__name__ for result in results) == ["NoneType", "RuntimeError"]
    assert watcher.running
    await watcher.stop()


@pytest.mark.asyncio
async def test_application_owns_watcher_lifecycle(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    write_settings(file)
    app = Application(ApplicationConfig(name="devtools-lifecycle"))
    app.config.register("database", Settings(database=Database(host="db-a", password="secret")))
    watcher = ConfigWatcher(app.config, "database", Settings, file, interval=0.05)
    app.register_config_watcher(watcher)

    await app.startup()
    assert watcher.running
    await app.stop()

    assert not watcher.running


def test_constructor_rejects_invalid_configuration_and_bounds(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    write_settings(file)
    config = make_config()

    for interval in (float("nan"), float("inf"), 0, 0.001, 3_600.1, True, "1"):
        with pytest.raises(ValueError):
            ConfigWatcher(config, "database", Settings, file, interval=interval)
    for section in ("Database", "x" * 64):
        with pytest.raises(ValueError, match="lowercase identifiers"):
            ConfigWatcher(config, section, Settings, file)
    for prefix in ("x" * 256, "APP\n"):
        with pytest.raises(ValueError, match="bounded printable"):
            ConfigWatcher(config, "database", Settings, file, prefix=prefix)
    with pytest.raises(ValueError, match="64 MiB"):
        ConfigWatcher(config, "database", Settings, file, max_file_bytes=64 * 1024 * 1024 + 1)
    with pytest.raises(TypeError, match="Config owner"):
        ConfigWatcher(object(), "database", Settings, file)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Pydantic model"):
        ConfigWatcher(config, "database", object, file)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="pathlib.Path"):
        ConfigWatcher(config, "database", Settings, str(file))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_concurrent_manual_polls_are_serialized(tmp_path: Path, monkeypatch) -> None:
    file = tmp_path / "config.toml"
    write_settings(file)
    watcher = ConfigWatcher(make_config(), "database", Settings, file)
    assert await watcher.run_once() is None
    write_settings(file, "db-b")
    active = 0
    peak = 0
    original_load = watcher._load

    async def tracked_load():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return await original_load()
        finally:
            active -= 1

    monkeypatch.setattr(watcher, "_load", tracked_load)
    await asyncio.gather(watcher.run_once(), watcher.run_once())

    assert peak == 1
    assert watcher._config.get("database").database.host == "db-b"
