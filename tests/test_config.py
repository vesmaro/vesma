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


# ── nhi-3 secure defaults: the compression-automation knob pair ──────────────


def test_autocompression_valid_pair_and_defaults_construct() -> None:
    """nhi-3 (ArchCom security verdict cond. 1+2, pin precedent mint-protection
    #432): the compression-automation knob PAIR is legal when complete —
    ``hooks.auto_compress=true`` together with ``ccr.validate_markers=true``
    in the SAME Settings — and the factory defaults (both False) stay valid."""
    from vesma.config import Settings

    settings = Settings(
        ccr={"validate_markers": True},
        hooks={"auto_compress": True},
    )
    assert settings.hooks.auto_compress is True
    assert settings.ccr.validate_markers is True

    defaults = Settings()
    assert defaults.hooks.auto_compress is False
    assert defaults.ccr.validate_markers is False


def test_autocompression_without_strict_markers_rejected() -> None:
    """nhi-3: ``hooks.auto_compress=true`` with the strict gate off is refused
    at the config boundary — the error carries the operator recipe verbatim."""
    import pytest
    from pydantic import ValidationError

    from vesma.config import Settings

    with pytest.raises(ValidationError, match=r"ccr\.validate_markers=true"):
        Settings(
            ccr={"validate_markers": False},
            hooks={"auto_compress": True},
        )
    # Strict gate off is the default — an omitted section must fail too.
    with pytest.raises(ValidationError, match=r"hooks\.auto_compress=true requires"):
        Settings(hooks={"auto_compress": True})


def test_autocompression_gate_sees_merged_config_from_any_source(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    """nhi-3 (verdict §5 cond. 2): the pair is validated in the SAME config —
    the gate reads the MERGED Settings values regardless of which source
    supplied them, so env-layer enablement cannot dodge a file-level refusal."""
    import pytest
    from pydantic import ValidationError

    from vesma.config import Settings

    monkeypatch.delenv("VESMA_HOOKS__AUTO_COMPRESS", raising=False)
    monkeypatch.delenv("VESMA_CCR__VALIDATE_MARKERS", raising=False)
    monkeypatch.setenv("VESMA_HOOKS__AUTO_COMPRESS", "true")
    with pytest.raises(ValidationError, match=r"ccr\.validate_markers=true"):
        Settings()
    # The same merged config flips valid once the env carries the pair.
    monkeypatch.setenv("VESMA_CCR__VALIDATE_MARKERS", "true")
    assert Settings().hooks.auto_compress is True
    assert Settings().ccr.validate_markers is True


def test_autocompression_gate_fires_on_load_settings_both_surfaces(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """nhi-3: MCP and HTTP construct their manager through the single
    ``load_settings()`` funnel (vesma/mcp_server.py ``_get_manager`` and
    vesma/api/main.py ``lifespan``), so a bad YAML pair is refused
    identically on both surfaces — the validation lives in Settings, one
    boundary, no per-surface drift."""
    import pytest
    from pydantic import ValidationError

    from vesma.config import load_settings

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "hooks:\n  auto_compress: true\nccr:\n  validate_markers: false\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match=r"ccr\.validate_markers=true"):
        load_settings(cfg)
