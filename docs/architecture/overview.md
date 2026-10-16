# Orbit DevTools architecture

`orbit-devtools` is an optional local-development package. Its current implementation is
`ConfigWatcher`, which polls one TOML-backed configuration file, validates a named Pydantic
section, and submits changed values through Core's normal `Config.reload_section()` observer path.
Applications that do not install and register the package perform no file polling.

```text
TOML file ── bounded Core parser ── Pydantic model ── Config.reload_section()
                                                        └── observer accept/veto
```

The watcher is deliberately outside Core: local reload behavior is optional developer tooling,
while Core owns configuration contracts, validation and atomic replacement, observers, and
application lifecycle. The package does not add a file watcher daemon, web framework, remote
configuration service, or provider SDK.

## Initial observation and updates

The watcher fingerprints a bounded file snapshot using filesystem metadata and a SHA-256 content
digest. The first observation is parsed and validated but does not mutate the already-composed
configuration section. Later changed snapshots are loaded through Core's `load_config()` with the
configured values/environment/prefix inputs and the supplied model. Before applying the candidate,
the watcher fingerprints the file again; a file changed during parsing is rejected and picked up on
a later poll.

Core's `reload_section()` performs the atomic replacement and invokes observers. If validation or
an observer rejects the candidate, the previous section remains active and the watcher exposes only
a generic `last_error` marker. A successful replacement advances the observed fingerprint. This is
poll-based eventually observed local reload, not a filesystem event subscription or a distributed
configuration protocol.

## Bounds and ownership

The watcher accepts one lowercase section identifier, a bounded file size (default 1 MiB, at most
64 MiB), a poll interval from 50 ms to one hour, and Core's bounded configuration prefix. File
reads, hashing, and parsing run through `asyncio.to_thread()` so they do not block the application
event loop. Polls on one watcher instance are serialized.

Core's application owns watcher lifecycle when registered via `register_config_watcher()`. The
watcher starts after the application is composed and stops during shutdown. It does not change the
initial configuration merely because a file exists. The watcher owns no file descriptor between
polls and creates no child process or network connection.

## Intended boundary

This helper targets local development. Production configuration should use the deployment's
validated, access-controlled configuration and secret-delivery process; polling a mutable local
file does not provide distributed consistency, authenticated change history, atomic cross-instance
rollout, or secret rotation guarantees.
