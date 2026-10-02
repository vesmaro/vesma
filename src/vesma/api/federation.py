"""FastAPI router for the federation mediated-pull endpoint (Phase 2).

ArchCom 2026-07-17 federation contract §3.2. Registers
``POST /api/v1/federation/pull`` on the existing Vesma FastAPI app.
The route is a thin adapter over :func:`vesma.federation_server.handle_pull`
— no business logic lives here.

Auth (ADR-0016): the route reads the bearer token from the
``Authorization`` header and passes it to ``handle_pull`` as
``presented_token``. The optional mTLS client-cert fingerprint is
read from the ``X-Client-Cert-Fingerprint`` header (the reverse proxy
is responsible for setting it when mTLS termination is upstream —
see :func:`vesma.federation_server.verify_mtls_fingerprint`).
"""

from __future__ import annotations

import logging
from typing import Annotated, cast

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from vesma.config import load_settings
from vesma.federation_access_log import DEFAULT_LOG_PATH, FederationAccessLog
from vesma.federation_server import PullRequest, PullResponse, handle_pull
from vesma.manager import MemoryManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/federation", tags=["federation"])

# Module-level access log — one per process. The path is resolved
# lazily so import never touches the filesystem. Tests override this
# via ``app.state.federation_access_log``.
_access_log: FederationAccessLog | None = None


def get_access_log(request: Request) -> FederationAccessLog:
    """Return the access log — from app.state or the module singleton.

    Resolution order:
    1. ``app.state.federation_access_log`` — tests / operator override.
    2. ``settings.federation.access_log_path`` — operator-configured
       path (e.g. ``/data/federation-access.jsonl`` for persistent
       volumes in containerised deployments).
    3. :data:`DEFAULT_LOG_PATH` — ``~/.mnemos/logs/federation-access.jsonl``.
    """
    log = getattr(request.app.state, "federation_access_log", None)
    if log is not None:
        return cast(FederationAccessLog, log)
    settings = get_settings(request)
    fed_cfg = getattr(settings, "federation", None)
    configured_path = getattr(fed_cfg, "access_log_path", None) if fed_cfg else None
    global _access_log
    if _access_log is None:
        path = configured_path if configured_path else DEFAULT_LOG_PATH
        _access_log = FederationAccessLog(path)
    return _access_log


def get_manager(request: Request) -> MemoryManager:
    """Return the MemoryManager — from app.state or the module singleton.

    Mirrors :func:`vesma.api.main.get_manager` but is route-local so
    the federation router is self-contained and does not import the
    main module (avoids a circular import: main imports this router).
    """
    mgr = getattr(request.app.state, "federation_manager", None)
    if mgr is not None:
        return cast(MemoryManager, mgr)
    # Fall back to the main module's singleton when wired into the real app.
    from vesma.api import main as api_main

    mgr = api_main.get_manager()
    if mgr is None:  # pragma: no cover - lifespan always sets it
        mgr = MemoryManager(load_settings())
    return mgr


def get_settings(request: Request) -> object:
    """Return the Settings — from app.state or load fresh."""
    s = getattr(request.app.state, "federation_settings", None)
    if s is not None:
        return s
    return load_settings()


@router.post("/pull", response_model=PullResponse)
async def federation_pull(
    payload: PullRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_client_cert_fingerprint: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Federation mediated-pull endpoint (contract §3.2).

    See :func:`vesma.federation_server.handle_pull` for the flow. The
    route extracts the bearer token from ``Authorization: Bearer <token>``
    and the optional mTLS fingerprint from
    ``X-Client-Cert-Fingerprint`` (set by the reverse proxy when mTLS
    termination is upstream — ADR-0016).
    """
    presented_token = None
    if authorization is not None:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            presented_token = value

    settings = get_settings(request)
    manager = get_manager(request)
    access_log = get_access_log(request)

    response, http_status = handle_pull(
        payload,
        settings=settings,
        manager=manager,
        access_log=access_log,
        presented_token=presented_token,
        presented_mtls_fingerprint=x_client_cert_fingerprint,
    )
    if http_status == status.HTTP_200_OK:
        return JSONResponse(status_code=http_status, content=response.model_dump(mode="json"))
    # Non-200: return the PullResponse body (with trigger_code) and the
    # matching HTTP status. RFC-reserved: the body is still a valid
    # PullResponse so the A-side client can parse trigger_code from a
    # refusal body.
    raise HTTPException(
        status_code=http_status,
        detail=response.model_dump(mode="json"),
    )
