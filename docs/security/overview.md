# Orbit DevTools security guidance

`ConfigWatcher` reads a local file and can replace a live configuration section after model
validation and Core observer approval. Treat write access to that file and authority to register the
watcher as configuration-changing privileges.

## File and process access

Store development configuration in a workspace with appropriate filesystem permissions. Do not
watch files writable by untrusted users or processes, and do not rely on path secrecy. Use a trusted
deployment configuration and secret manager for production. Avoid symlink or mount arrangements
that let a less-trusted actor replace the watched file with another source. The watcher does not
provide file ownership checks, signatures, encryption, or secure secret storage.

The initial file is parsed for validation but does not replace the application's composed section.
Subsequent valid edits can change live configuration, subject to Core's reload observers. Review
which values may be changed dynamically and ensure observers enforce immutable/security-sensitive
settings as needed.

## Secret values and diagnostics

TOML files, injected values, environment mappings, and resulting Pydantic models can contain
credentials. Protect them with local access controls and avoid committing them. The watcher caps
file size and exposes a generic `last_error` rather than paths, values, or observer exception text;
Core or application code may still log configuration changes. Do not log secret fields or place
them in `ConfigChange` diagnostics, metrics labels, or support bundles.

The file fingerprint contains a SHA-256 content digest held internally to detect same-size edits;
it is not a signature, confidentiality control, or provenance proof. A local actor with write access
can change the file and cause an otherwise valid model to be proposed.

## Scope

This package is local development tooling. It has no remote listener, authentication, authorization,
distributed configuration protocol, or secret rotation service. Follow the repository
[security policy](../../SECURITY.md) to report vulnerabilities; do not attach real secrets or
confidential configuration to public issues.
