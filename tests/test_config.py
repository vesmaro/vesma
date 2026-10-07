def test_search_hybrid_alpha_default_is_tuned() -> None:
    """#300: the fusion weight default is 0.5 (balanced RRF) — the re-tune
    is a registered composition change; this pin fails on any silent
    re-tune of the retrieval behavior the benches and baselines assume."""
    from vesma.config import SearchConfig

    assert SearchConfig().hybrid_alpha == 0.5


def test_federation_project_lists_reject_degenerate_slugs() -> None:
    """ACL hardening review MAJOR: blank/``'*'``-in-shared slugs are refused.

    ``''`` in either list would match every UNTAGGED record in SQL
    (``project IN ('')`` against the ``DEFAULT ''`` column); ``'*'`` in
    ``shared_projects`` would flow into ``_intersect_projects`` and hand
    a scoped read ANY project verbatim (read/write asymmetry). Both are
    config-boundary failures — the process refuses to boot with them.
    """
    import pytest
    from pydantic import ValidationError

    from vesma.config import FederationConfig, PeerConfig

    # Degenerate shared_projects: wildcard and blanks.
    with pytest.raises(ValidationError, match=r"shared_projects.*\*"):
        FederationConfig(shared_projects=["*"])
    with pytest.raises(ValidationError, match="blank project slug"):
        FederationConfig(shared_projects=[""])
    with pytest.raises(ValidationError, match="blank project slug"):
        FederationConfig(shared_projects=["alpha", " "])

    # Degenerate allowed_projects: blanks ('*' stays legal — documented grant).
    with pytest.raises(ValidationError, match="blank project slug"):
        PeerConfig(bearer_token_env="T", allowed_projects=[""])

    # Legitimate values still construct: normal slugs, per-peer wildcard.
    FederationConfig(shared_projects=["alpha", "beta"])
    PeerConfig(bearer_token_env="T", allowed_projects=["*", "alpha"])


def test_legacy_config_spellings_rejected_clean_slate(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """6.0 clean-slate contract: the pre-rebrand spellings are REJECTED, not
    silently accepted — the ``mnemos=`` Settings section dies on
    ``extra=forbid``, and the retired ``VESMARO_*``/``MNEMOS_*`` env
    prefixes (#471 dual-read period) never reach the model again. The only
    canonical spellings are the ``vesma`` section and the ``VESMA_`` prefix."""
    import pytest
    from pydantic import ValidationError

    from vesma.config import Settings

    with pytest.raises(ValidationError, match="mnemos"):
        Settings(mnemos={"vault_path": "/tmp/vault"})  # type: ignore[call-arg]

    monkeypatch.delenv("VESMA_AWARENESS__NATIVE_HEARTBEAT_MODE", raising=False)
    monkeypatch.setenv("VESMARO_AWARENESS__NATIVE_HEARTBEAT_MODE", "canary")
    monkeypatch.setenv("MNEMOS_AWARENESS__NATIVE_HEARTBEAT_MODE", "canary")
    assert Settings().awareness.native_heartbeat_mode == "off"
