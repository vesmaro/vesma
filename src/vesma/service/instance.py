"""Instance identity and endpoint parameterization (ADR-0049 Lock 3, wave 1).

ONE vesma instance = (store home, endpoint) — contract §3. This module is
THE choke point of the endpoint half:

- **instance-id** — minted at install (the registry writers call
  :func:`ensure_instance_id`), stable across restarts (one ``0600`` file
  in the store home: the identity follows the data through a mover
  transit, by design), carried by the control ``hello`` frame and the
  ``X-Vesma-Instance-Id`` API response header, and recorded in
  ``instances.json`` (the wave-0 ``instance_id: None`` placeholder).

- **endpoint parameterization** — the control socket of a NON-default
  instance lives at ``${XDG_RUNTIME_DIR}/vesma/instances/<id>/control.sock``
  (the §3.6 state-home fallback mirrors the same shape); the mesh unix
  socket and the API/mesh ports come from the instance's own config.
  The DEFAULT instance — no ``VESMA_INSTANCE_ID`` in the environment —
  keeps every today's path, port 8787 and the canonical control socket
  byte-for-byte (backward compatibility is the contract's §3 mandate,
  pinned by tests).

Runtime identity resolution (the process, not the CLI):

1. ``VESMA_INSTANCE_ID`` (set by the ``vesma@<id>.service`` template
   unit as ``Environment=VESMA_INSTANCE_ID=%i``) — validated, wins;
2. the persisted ``<home>/instance-id`` file;
3. a freshly minted id, persisted immediately (the migration path of
   every pre-wave-1 install: the first boot after the upgrade mints
   once and is stable thereafter).

An env id and a persisted id that DISAGREE are a misconfiguration (two
template instances pointed at one home): :class:`InstanceHomeMismatchError`
refuses the start fail-loud — this is the wave-1 half of the
«two instances over one home» guard (the kernel-level half is the
wave-0 per-home flock, which refuses the second WRITER regardless of
identity).

Deliberately NOT here: unit generation (the ``vesma@.service`` template
lives in :mod:`vesma.service.unitgen`), the registry file format
(:mod:`vesma.service.instance_registry`), CLI write-lock policy (the
f1c4dbc7 question is a design answer in the wave report, not code).
"""

from __future__ import annotations

import contextlib
import os
import re
import socket
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from vesma.fs_hardening import ensure_private_dir

#: The reserved id of the default (backward-compatible) instance. An
#: explicit ``VESMA_INSTANCE_ID=default`` behaves exactly like an unset
#: variable — canonical paths everywhere.
DEFAULT_INSTANCE_ID = "default"

#: The template-unit env var (``Environment=VESMA_INSTANCE_ID=%i``). Flat
#: name, canonical ``VESMA_`` prefix — the same convention as
#: ``VESMA_CONFIG``.
INSTANCE_ENV_VAR = "VESMA_INSTANCE_ID"

#: The persisted identity file: one line inside the STORE HOME.
ID_FILE_NAME = "instance-id"

#: Charset of a non-default id: systemd-template safe (``%i`` needs no
#: escaping), filesystem safe, journald-safe. Same discipline as
#: ``COMPONENT_NAME_RE`` (control-socket §4.7), digits allowed first.
INSTANCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

#: The API response header carrying the instance id (contract Lock 3).
INSTANCE_HEADER = "X-Vesma-Instance-Id"

_ID_FILE_MODE = 0o600


class InstanceError(RuntimeError):
    """Base class of the instance-identity failures (fail-loud)."""


class InstanceIdError(InstanceError):
    """A candidate id violates the charset/length contract."""


class InstanceHomeMismatchError(InstanceError):
    """Two different identities claim one store home.

    The runtime env id (template unit) and the home's persisted id
    disagree — a template instance pointed at a foreign home. The start
    is refused; the message names both identities and the home.
    """


class EndpointBusyError(InstanceError):
    """The API/mesh port is already served by another process.

    The message names the holder when the holder identifies itself (a
    live vesma instance answers ``/health`` with
    :data:`INSTANCE_HEADER`), else reports an unattributed holder.
    """


def validate_instance_id(raw: str) -> str:
    """Validate one candidate id; ``default`` normalizes to
    :data:`DEFAULT_INSTANCE_ID`. Raises :class:`InstanceIdError`."""
    value = raw.strip()
    if value == DEFAULT_INSTANCE_ID:
        return DEFAULT_INSTANCE_ID
    if not INSTANCE_ID_RE.match(value):
        raise InstanceIdError(
            f"instance id {raw!r} is invalid — must match {INSTANCE_ID_RE.pattern!r} "
            "(systemd-template, filesystem and journald safe)"
        )
    return value


def resolve_requested_instance_id(env: dict[str, str] | None = None) -> str | None:
    """The id the ENVIRONMENT requests: ``None`` = the default instance.

    Pure env reading — no I/O, no minting. ``None`` (unset or empty) is
    the load-bearing default: every canonical path/port of today's
    install hangs off it. Test seam: ``env`` overrides ``os.environ``.
    """
    raw = (env if env is not None else os.environ).get(INSTANCE_ENV_VAR, "")
    if not raw.strip():
        return None
    return validate_instance_id(raw)


def id_file_path(data_dir: Path) -> Path:
    """``<store home>/instance-id`` — the persisted identity file."""
    return data_dir / ID_FILE_NAME


def read_persisted_instance_id(data_dir: Path) -> str | None:
    """The home's persisted id, or ``None`` when not yet minted.

    A PRESENT but invalid file raises :class:`InstanceIdError` — silently
    re-minting over a damaged identity file would re-identify the home
    (registry rows, hello frames and journal attribution would split);
    fail-loud keeps the operator in charge of the deletion.
    """
    path = id_file_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return validate_instance_id(raw)


def _persist_instance_id(data_dir: Path, value: str) -> str:
    """Atomically create the id file EXCLUSIVELY; a racing creator's value
    wins and is returned instead (O_EXCL — the mint is race-safe by
    construction, no lock needed)."""
    ensure_private_dir(data_dir)
    path = id_file_path(data_dir)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _ID_FILE_MODE)
    except FileExistsError:
        existing = read_persisted_instance_id(data_dir)
        assert existing is not None  # O_EXCL loser: the winner's file exists
        return existing
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    return value


def _minted_id() -> str:
    """A fresh id: 12 hex chars — short enough for unit names, long
    enough to never collide on a single machine."""
    return uuid.uuid4().hex[:12]


def ensure_instance_id(data_dir: Path, *, env_value: str | None = None) -> str:
    """The home's instance id: env wins, persisted file next, mint last.

    - ``env_value`` (the validated ``VESMA_INSTANCE_ID``) present and the
      file agrees → the env id;
    - env present, file absent → the env id is PERSISTED (the template
      instance's first boot claims the home);
    - env present, file disagrees → :class:`InstanceHomeMismatchError`;
    - no env → the file, or a fresh mint persisted immediately.

    Stable across restarts by construction: minting happens exactly once
    per home.
    """
    persisted = read_persisted_instance_id(data_dir)
    if env_value is None:
        if persisted is not None:
            return persisted
        return _persist_instance_id(data_dir, _minted_id())
    requested = validate_instance_id(env_value)
    if persisted is None:
        return _persist_instance_id(data_dir, requested)
    if persisted != requested:
        raise InstanceHomeMismatchError(
            f"instance identity mismatch on store home {data_dir}: the environment "
            f"claims {requested!r} but the home is persisted as {persisted!r} — two "
            "instances must not share one store home (ADR-0049 Lock 3); point the "
            "template unit at the instance's own home or remove the stale "
            f"{id_file_path(data_dir)} after confirming it is orphaned"
        )
    return requested


def runtime_instance_dir(runtime_root: Path, instance_id: str | None) -> Path:
    """The instance's runtime directory under a resolved runtime root.

    ``None``/``default`` → the root itself (today's canonical socket dir);
    any other id → ``<root>/instances/<id>/`` (contract wave 1: the
    control socket leaves the shared root).
    """
    if instance_id is None or instance_id == DEFAULT_INSTANCE_ID:
        return runtime_root
    return runtime_root / "instances" / instance_id


def preflight_api_port(host: str, port: int, *, timeout_s: float = 1.0) -> None:
    """Typed port-conflict refusal (card ff6a9385, wave 1 mechanics).

    A TCP connect that FAILS means the port is free — return. A connect
    that SUCCEEDS means someone already serves the port: probe the
    holder over HTTP ``/health`` and raise :class:`EndpointBusyError`
    naming the vesma instance when the holder identifies itself via
    :data:`INSTANCE_HEADER`, else naming an unattributed holder. The
    refusal happens BEFORE the writer lock and any bind, so the operator
    sees the holder's name instead of a raw uvicorn bind traceback (or a
    30-second supervisor startup timeout).

    Port ``0`` (ephemeral — tests only) never conflicts: skipped.
    """
    if port <= 0:
        return
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(timeout_s)
    try:
        probe.connect((host, port))
    except OSError:
        return  # nothing is listening — the port is ours to bind
    finally:
        probe.close()
    holder = _identify_http_holder(host, port)
    detail = (
        f"a live vesma instance (id: {holder!r}) answered on http://{host}:{port}"
        if holder is not None
        else f"another process holds http://{host}:{port} (no vesma API answered "
        "the identity probe)"
    )
    raise EndpointBusyError(
        f"API port conflict: {detail} — give this instance its own api.port in "
        "its config (two instances must not share one endpoint; ADR-0049 §3)"
    )


def _identify_http_holder(host: str, port: int) -> str | None:
    """The holder's instance id via ``/health`` + the instance header, or
    ``None`` when the holder does not identify itself (best-effort
    attribution — the refusal itself never depends on the probe)."""
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(  # nosec B310 - loopback identity probe of the configured bind address
            url, timeout=2.0
        ) as response:
            raw: object | None = response.headers.get(INSTANCE_HEADER)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()


def effective_mesh_socket_path(settings: Any) -> str:
    """The mesh unix socket of THIS instance (card ba729a40, wave 1).

    Resolution (first match wins):

    1. an explicitly configured ``mesh.socket_path`` (config file or env
       — ``model_fields_set`` tells explicit from default) — honored
       verbatim, whatever the profile;
    2. the SYSTEM profile — the FHS IPC dir ``/run/vesma/core.sock``:
       the historical Linux default ``/run/mnemos/core.sock`` is the
       USER profile's tmpfiles convention and is not writable by the
       system service account (the 2026-10-28 fail-loop finding);
    3. a non-default user instance — its own runtime dir
       ``.../instances/<id>/core.sock`` (endpoint isolation, wave 1);
    4. otherwise — the configured default, byte-for-byte today's value
       (backward compatibility).

    ``settings`` is typed ``Any`` to keep this module import-light: the
    duck-typed surface is ``settings.mesh.socket_path`` +
    ``model_fields_set`` (the :class:`vesma.config.MeshConfig` shape).
    """
    mesh: Any = settings.mesh
    if "socket_path" in mesh.model_fields_set:
        return str(mesh.socket_path)
    from vesma.service import fhs, layout

    if layout.system_profile_active():
        return str(fhs.system_ipc_dir() / "core.sock")
    requested = resolve_requested_instance_id()
    if requested is not None:
        resolution = layout.resolve_runtime_dir()
        return str(runtime_instance_dir(resolution.path, requested) / "core.sock")
    return str(mesh.socket_path)


__all__ = [
    "DEFAULT_INSTANCE_ID",
    "ID_FILE_NAME",
    "INSTANCE_ENV_VAR",
    "INSTANCE_HEADER",
    "EndpointBusyError",
    "InstanceError",
    "InstanceHomeMismatchError",
    "InstanceIdError",
    "effective_mesh_socket_path",
    "ensure_instance_id",
    "id_file_path",
    "preflight_api_port",
    "read_persisted_instance_id",
    "resolve_requested_instance_id",
    "runtime_instance_dir",
    "validate_instance_id",
]
