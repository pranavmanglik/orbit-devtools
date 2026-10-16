# Orbit DevTools operations

`ConfigWatcher` is an opt-in local development helper. Register it with the owning Core application
after registering and freezing the configuration section it observes. The app starts and stops the
watcher with its lifecycle. Do not create several watchers for the same section/file unless multiple
independent reload attempts are intentional.

## Inputs and polling

The watcher accepts a `Config`, lowercase section name, Pydantic model class, and `pathlib.Path`.
Optional `values`, `environment`, and `prefix` are passed to Core's `load_config()`; they are not
separate watcher-specific configuration stores. The default poll interval is one second, bounded
from 50 ms to one hour. The default file limit is 1 MiB, configurable up to 64 MiB. Lower limits
reduce hashing/parsing cost and memory pressure. Avoid extremely short intervals in development
because each poll performs filesystem work and computes a digest.

The watcher polls rather than receiving filesystem events. It reads and hashes a bounded file in a
worker thread, parses away from the event loop, and checks the fingerprint again before publishing
a candidate. An edit during parsing is rejected and will be observed on a later pass. Multiple
manual `run_once()` calls on the same instance serialize with one another.

## Startup and reload behavior

The initial file is validated but does not replace the current configured section. A subsequent
changed file is applied through Core's `reload_section()` and its observers. An invalid model or
observer veto preserves the last accepted section. `last_error` returns a generic error without
the path, parsed values, or original observer exception details. Check this property for local
developer feedback; it is not a health endpoint or detailed diagnostics channel.

## Shutdown and limitations

`stop()` signals the polling task and waits for an in-progress bounded file operation to finish.
Core waits for watcher shutdown as part of its lifecycle. Keep file limits and poll intervals
reasonable so local shutdown remains prompt.

This helper does not watch directories, remote configuration sources, secrets rotation, or multiple
application instances. It provides no distributed rollout, audit log, access-control policy,
network endpoint, or production configuration control plane. Production deployments should use
their established configuration and secret-management systems.
