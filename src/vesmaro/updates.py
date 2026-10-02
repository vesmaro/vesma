"""Update check + self-update primitives (issue #445).

One quiet question — "is there a newer release than what I run?" — answered
from PyPI with a 24h disk cache, plus the install-side helpers the
``vesma update`` CLI command drives (history append, systemd user-timer
units live in :mod:`vesmaro.cli.update_cmd`).

Design contract:

* **Never raises, never blocks callers.** Every public entry point wraps
  its work in ``try/except`` and degrades to ``None``. The only network
  call is a stdlib ``urllib.request`` GET of ``pypi.org/pypi/<dist>/json``
  with a 3s timeout.
* **Privacy.** A version check is one GET of a public version manifest —
  no telemetry, no machine identifiers, nothing posted.
* **Offline machines are unaffected.** Beyond the no-raise contract, a
  failed fetch writes a NEGATIVE cache entry (TTL 1h) so a black-holed
  network costs at most one 3s timeout per hour instead of per call;
  a previously cached ``latest`` is still served with ``stale=True``.
* **Opt-out, two independent switches.** Config ``updates.check_enabled``
  (default ``true``; env ``VESMA_UPDATES__CHECK_ENABLED``) and the hard
  env override ``VESMA_UPDATES_CHECK`` (``off``/``0``/``false``/``no``).
  Either being off disables the check entirely.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib.metadata import version as _md_version
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vesmaro import __version__

if TYPE_CHECKING:
    from vesmaro.config import Settings

logger = logging.getLogger(__name__)

PYPI_JSON_URL = "https://pypi.org/pypi/{dist}/json"
HTTP_TIMEOUT_SEC = 3.0
CACHE_FILENAME = "update-check.json"
CACHE_TTL = timedelta(hours=24)
NEGATIVE_CACHE_TTL = timedelta(hours=1)
HISTORY_FILENAME = "update-history.json"
FALLBACK_UPDATE_DIR = Path("~/.local/share/vesma").expanduser()

#: Pip distributions that can carry this server, in upgrade-priority order
#: (first hit wins — this is the dist ``vesma update`` pip-upgrades).
CANDIDATE_DISTS = ("vesma-memory-server", "vesma")

#: Hard env opt-out (independent of the config knob). Canonical ``VESMA_``
#: name; the deprecated ``VESMARO_UPDATES_CHECK`` spelling stays honoured
#: until 6.0 (dual-prefix contract, ADR-0031 pattern).
OPT_OUT_ENV = "VESMA_UPDATES_CHECK"
_DEPRECATED_OPT_OUT_ENV = "VESMARO_UPDATES_CHECK"
_OFF_VALUES = frozenset({"off", "0", "false", "no"})


@dataclasses.dataclass(frozen=True)
class UpdateInfo:
    """Result of one update check.

    ``stale`` marks an answer served from a cache that could not be
    refreshed (network failure) — advisory data, honest provenance.
    """

    installed: str
    latest: str
    dist: str
    update_available: bool
    checked_at: str
    stale: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ── version comparison (stdlib-only, PEP 440 release-segment shaped) ─────────


def version_key(version: str) -> list[tuple[int, int, str]]:
    """Sort key over dotted release segments.

    Numeric segments compare numerically, non-numeric ones lexically after
    all numeric segments of the same position; a longer release wins when
    the common prefix ties (``1.2`` < ``1.2.0``). Advisory-grade only —
    it orders release candidates against finals consistently enough for
    "is there something newer", which is all this module promises.
    """
    return [
        (0, int(token), "") if token.isdigit() else (1, 0, token)
        for token in re.findall(r"\d+|[^\d.]+", version)
    ]


# ── opt-out ──────────────────────────────────────────────────────────────────


def env_check_disabled() -> bool:
    """True when ``VESMA_UPDATES_CHECK`` says off (case-insensitive).

    The deprecated ``VESMARO_UPDATES_CHECK`` spelling is honoured as a
    fallback (accepted until 6.0).
    """
    value = os.environ.get(OPT_OUT_ENV) or os.environ.get(_DEPRECATED_OPT_OUT_ENV, "")
    return value.strip().lower() in _OFF_VALUES


# ── installed dist detection ─────────────────────────────────────────────────


def detect_installed_dist() -> tuple[str, str] | None:
    """Return ``(dist, installed_version)`` for the first present dist.

    ``None`` means no pip-installed distribution was found (source
    checkout) — nothing for ``pip --user`` to upgrade, so the check is
    meaningless and callers get ``None``.
    """
    for dist in CANDIDATE_DISTS:
        try:
            return dist, _md_version(dist)
        except Exception:
            continue
    return None


# ── PyPI fetch (the only network leg) ────────────────────────────────────────


def fetch_latest(dist: str) -> str:
    """GET the PyPI JSON manifest and return ``info.version``.

    Raises on any network/parse failure — :func:`check_for_update` is the
    never-raises boundary.
    """
    request = urllib.request.Request(
        PYPI_JSON_URL.format(dist=dist),
        headers={"User-Agent": f"vesma/{__version__} (update-check)"},
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SEC) as response:
        payload = json.load(response)
    latest = payload["info"]["version"]
    if not isinstance(latest, str) or not latest:
        raise ValueError(f"pypi manifest for {dist!r} carries no usable info.version")
    return latest


# ── cache sidecar ────────────────────────────────────────────────────────────


def resolve_cache_path(settings: Settings | None = None) -> Path:
    """``<data_dir>/update-check.json`` (the standard settings plumbing).

    Falls back to ``~/.local/share/vesma/update-check.json`` when settings
    cannot be loaded (a broken config must not break a version check).
    """
    try:
        if settings is None:
            from vesmaro.config import load_settings

            settings = load_settings()
        return Path(settings.mnemos.data_dir).expanduser() / CACHE_FILENAME
    except Exception:
        return FALLBACK_UPDATE_DIR / CACHE_FILENAME


def _write_cache(
    path: Path,
    *,
    dist: str,
    installed: str,
    latest: str | None,
    ok: bool,
    now: datetime,
) -> None:
    """Best-effort cache write; failures are logged and swallowed.

    ``ok=False`` is the NEGATIVE cache: the fetch attempt failed at
    ``checked_at``. When a previous ``latest`` is known it is preserved so
    stale answers stay available inside the negative-TTL window.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": 1,
            "checked_at": now.isoformat(),
            "dist": dist,
            "installed": installed,
            "latest": latest,
            "ok": ok,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        logger.debug("update-check cache write failed", exc_info=True)


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != 1:
            return None
        return payload
    except Exception:
        return None


def _cache_fresh(payload: dict[str, Any], now: datetime) -> bool:
    try:
        checked_at = datetime.fromisoformat(str(payload["checked_at"]))
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=UTC)
        age = now - checked_at
        return age < (CACHE_TTL if payload.get("ok") else NEGATIVE_CACHE_TTL)
    except Exception:
        return False


def _info_from_cache(payload: dict[str, Any], dist: str, installed: str) -> UpdateInfo | None:
    latest = payload.get("latest")
    if not isinstance(latest, str) or not latest:
        return None
    return UpdateInfo(
        installed=installed,
        latest=latest,
        dist=dist,
        update_available=version_key(latest) > version_key(installed),
        checked_at=str(payload.get("checked_at", "")),
        stale=not payload.get("ok", True),
    )


# ── the public check ─────────────────────────────────────────────────────────


def check_for_update(
    settings: Settings | None = None,
    *,
    cache_path: Path | None = None,
    fetcher: Callable[[str], str] | None = None,
    now: datetime | None = None,
) -> UpdateInfo | None:
    """Check PyPI for a newer release of the installed dist. Never raises.

    Cache-first: a fresh (24h) cache answers without any network I/O; an
    expired cache triggers one 3s-capped GET; a failed GET serves the
    previous answer with ``stale=True`` and arms the 1h negative cache so
    offline machines pay the timeout at most once per hour. Any error —
    opt-out, missing dist, broken cache, network — returns ``None``.

    Self-upgrade drift (issue #460): a fresh positive cache whose
    ``latest`` is OLDER than the locally installed version predates the
    install and is re-checked synchronously (one capped GET); on failure
    the previous answer is served with ``stale=True``.

    ``cache_path``/``fetcher``/``now`` are injection points for tests.
    """
    try:
        if env_check_disabled():
            return None
        if settings is not None and not getattr(settings.updates, "check_enabled", True):
            return None
        if settings is None:
            # The config knob still applies when the caller had no settings
            # (e.g. `vesma --version`); a config-load failure falls through
            # to the default-on posture.
            try:
                from vesmaro.config import load_settings

                if not load_settings().updates.check_enabled:
                    return None
            except Exception:
                pass

        detected = detect_installed_dist()
        if detected is None:
            return None
        dist, installed = detected

        path = cache_path or resolve_cache_path(settings)
        moment = now or datetime.now(UTC)
        cached = _read_cache(path)
        if cached is not None and _cache_fresh(cached, moment):
            info = _info_from_cache(cached, dist, installed)
            if info is None or not cached.get("ok", True):
                # Unusable payload, or a fresh NEGATIVE-cache entry: the 1h
                # negative TTL already bounds the re-check cost — the cached
                # answer (possibly ``None``) stands, exactly as before #460.
                return info
            if version_key(installed) <= version_key(info.latest):
                return info
            # Installed is NEWER than the cached latest on a positive cache
            # (issue #460): a self-upgrade landed after this entry was
            # written — the cache predates the world change, so it is stale
            # by definition. Fall through to ONE synchronous re-check (the
            # fetch block below; on failure it keeps the previous answer
            # with ``stale=True``) and the caller marks the drift honestly.

        do_fetch = fetcher or fetch_latest
        try:
            latest = do_fetch(dist)
        except Exception:
            previous_latest = (cached or {}).get("latest")
            previous_at = (cached or {}).get("checked_at")
            _write_cache(
                path, dist=dist, installed=installed, latest=previous_latest, ok=False, now=moment
            )
            if isinstance(previous_latest, str) and previous_latest:
                logger.debug("update check failed — serving stale cache from %s", previous_at)
                # Stale BY DEFINITION here: the refresh just failed, even if
                # the cached entry itself was once a fresh positive answer.
                return UpdateInfo(
                    installed=installed,
                    latest=previous_latest,
                    dist=dist,
                    update_available=version_key(previous_latest) > version_key(installed),
                    checked_at=str(previous_at or ""),
                    stale=True,
                )
            return None

        info = UpdateInfo(
            installed=installed,
            latest=latest,
            dist=dist,
            update_available=version_key(latest) > version_key(installed),
            checked_at=moment.isoformat(),
        )
        _write_cache(path, dist=dist, installed=installed, latest=latest, ok=True, now=moment)
        return info
    except Exception:
        logger.debug("update check failed", exc_info=True)
        return None


def update_stats_payload(settings: Settings | None = None) -> dict[str, Any] | None:
    """The ``update_available`` object (or ``None``) for the stats payload."""
    try:
        info = check_for_update(settings)
    except Exception:
        return None
    return info.as_dict() if info is not None else None


def log_update_if_available(settings: Settings | None = None) -> None:
    """One INFO log line at server start when a newer release exists.

    Deliberately SYNCHRONOUS (issue #445 decision): the check is cached on
    disk after its first run, capped at the 3s HTTP timeout, and the
    server startup path has no ordering constraint it could disturb — a
    daemon thread would add lifetime management for no gain.
    """
    try:
        info = check_for_update(settings)
    except Exception:
        return
    if info is not None and info.update_available:
        logger.info(
            "update available: %s (installed %s) — run 'vesma update' to upgrade",
            info.latest,
            info.installed,
        )
