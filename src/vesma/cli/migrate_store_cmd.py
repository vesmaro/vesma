"""``vesma migrate-store`` CLI — the B3 store mover 5.x → 6.0 plus the
system-profile relocation target (ADR-0047).

Thin wiring over :mod:`vesma.store_migration` (all semantics live there;
see that module's docstring for the normative safety contracts).

Exit codes: 0 success · 2 usage error · 3 discovery refused (explicit
``--from`` required) · 4 quiesce violation · 5 snapshot failure ·
6 verification failure · 7 already migrated · 8 invalid source ·
9 config migration blocked · 10 system profile not installed ·
11 store content already in the system data root · 12 profile isolation
(DR-14) · 13 install-state drift (DR-17) · 14 source recovery required
(read-only probe, owner must repair) · 15 activation gate (--apply for
the system target before soak + owner's word) · 16 insufficient free
space.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from vesma.cli.util import console
from vesma.store_migration import (
    SYSTEM_TARGET,
    ActivationGateError,
    DiscoveryRefusedError,
    MigrationPlan,
    MigrationReport,
    StoreMigrationError,
    UsageError,
    activation_gate_open,
    build_plan,
    discover_source_candidates,
    run_migration,
)

_FROM_OPTION = Annotated[
    Path | None,
    typer.Option(
        "--from",
        help="REQUIRED explicit source store home (e.g. ~/.mnemos or a 6.0 "
        "~/.vesma for --target=system). Never guessed; omitted -> read-only "
        "discovery SUGGESTS candidates.",
    ),
]
_TO_OPTION = Annotated[
    Path | None,
    typer.Option(
        "--to",
        help="REQUIRED explicit target store home for the classic 5.x → 6.0 "
        "move (6.0 layout). Must not exist at all (even an empty directory "
        "is refused — choose a fresh target); snapshot lands next to it. "
        "NOT used with --target=system (that target is resolved from the "
        "system install-state + the FHS manifest).",
    ),
]
_APPLY_OPTION = Annotated[
    bool,
    typer.Option(
        "--apply",
        help="Write the migration. WITHOUT this flag the command is a "
        "dry-run: plan + counters via read-only opens; the source's "
        "records stay unchanged. For --target=system the apply is active "
        "since 6.5.0 (owner-ratified 2026-10-23 after the green soak, "
        "ADR-0045 Decision 9).",
    ),
]
_JSON_OPTION = Annotated[
    bool,
    typer.Option("--json", help="Machine-readable report (paths and numbers only)."),
]
_TARGET_OPTION = Annotated[
    str | None,
    typer.Option(
        "--target",
        help="'system': relocate a store INTO the installed system profile "
        "(data root + config plane resolved from the install-state and the "
        "FHS manifest; ADR-0047). Default: classic explicit --to move.",
    ),
]
_SNAPSHOT_DIR_OPTION = Annotated[
    Path | None,
    typer.Option(
        "--snapshot-dir",
        help="System target only: explicit snapshot location (default: next "
        "to the SOURCE, operator-owned 0700). Never inside the source or "
        "the target trees.",
    ),
]


def _forecast_dicts() -> list[dict[str, Any]]:
    """The C13 DR forecast serialized for ``--json`` — the SAME findings
    from ``system_target_forecast()`` (one implementation with
    ``vesma doctor``, same field shape as its JSON output)."""
    from vesma.service.doctor_checks import system_target_forecast

    return [
        {
            "check_id": f.check_id,
            "title": f.title,
            "severity": f.severity.value,
            "detail": f.detail,
            "fix_command": f.fix_command,
        }
        for f in system_target_forecast()
    ]


def _plan_payload(plan: MigrationPlan) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mode": plan.mode,
        "target": plan.target,
        "source_home": str(plan.source_home),
        "target_home": str(plan.target_home),
        "snapshot_dir": str(plan.snapshot_dir),
        "records": plan.stats.total,
        "status_breakdown": plan.stats.status_breakdown,
        "project_breakdown": plan.stats.project_breakdown,
        "tag_subtypes_to_rewrite": plan.stats.subtype_swaps,
        "project_slugs_to_rewrite": plan.stats.project_swaps,
        "duplicates_to_remove": plan.stats.duplicates_removed,
        "trust_markers_kept": plan.stats.trust_markers_kept,
        "vault_present": plan.vault_present,
        "config_present": plan.config_present,
        "fast_path": plan.fast_path,
        "source_wal_bytes": plan.wal_bytes,
        "quiesce_warnings": list(plan.quiesce_warnings),
        "planned_files": [
            {
                "source": str(f.source),
                "target": str(f.target),
                "size_bytes": f.size_bytes,
                "via_backup_api": f.via_backup_api,
            }
            for f in plan.planned_files
        ],
    }
    if plan.target == SYSTEM_TARGET:
        payload["data_root"] = str(plan.data_root)
        payload["system_config_path"] = str(plan.system_config_path)
        payload["remaining_cargo"] = list(plan.remaining_cargo)
        payload["precondition_lines"] = list(plan.precondition_lines)
        payload["secret_path_warnings"] = list(plan.secret_path_warnings)
        payload["config_drops"] = list(plan.config_drops)
        payload["forecast"] = _forecast_dicts()  # P3-4: C13 promised it in dry-run
    return payload


def _report_payload(report: MigrationReport) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mode": report.mode,
        "target": report.target,
        "source_home": str(report.source_home),
        "target_home": str(report.target_home),
        "snapshot_dir": str(report.snapshot_dir),
        "records": report.records,
        "tag_subtypes_rewritten": report.tag_subtypes_rewritten,
        "project_slug_rewritten": report.project_slug_rewritten,
        "duplicates_removed": report.duplicates_removed,
        "trust_markers_kept": report.trust_markers_kept,
        "status_breakdown": report.status_breakdown,
        "project_breakdown": report.project_breakdown,
        "files_copied": report.files_copied,
        "sqlite_via_backup_api": report.sqlite_via_backup_api,
        "fast_path": report.fast_path,
        "source_wal_bytes": report.wal_bytes,
        "verification": report.verification,
        "warnings": list(report.warnings),
    }
    if report.config is not None:
        payload["config"] = {
            "source_config": (
                str(report.config.source_config) if report.config.source_config else None
            ),
            "synthesized": report.config.synthesized,
            "section_renamed": report.config.section_renamed,
            "paths_rerooted": report.config.paths_rerooted,
            "db_name_pinned": report.config.db_name_pinned,
        }
    if report.target == SYSTEM_TARGET:
        payload["config_disposition"] = report.config_disposition
        payload["config_final_path"] = (
            str(report.config_final_path) if report.config_final_path else None
        )
        payload["ownership_applied"] = report.ownership_applied
        payload["remaining_cargo"] = list(report.remaining_cargo)
    return payload


def _print_plan(plan: MigrationPlan) -> None:
    stats = plan.stats
    console.print(f"[bold]migrate-store[/bold] — mode: [yellow]{plan.mode}[/yellow]")
    console.print(f"  target profile: {plan.target}")
    console.print(f"  from: {plan.source_home}")
    console.print(f"  to:   {plan.target_home}")
    console.print(f"  snapshot (on --apply): {plan.snapshot_dir}")
    if plan.target == SYSTEM_TARGET:
        console.print(f"  system config plane: {plan.system_config_path}")
    console.print()
    console.print("  [bold]Records[/bold]")
    console.print(f"    total:                 {stats.total}")
    for slug in sorted(stats.status_breakdown):
        console.print(f"    status={slug}:{' ' * (18 - len(slug))}{stats.status_breakdown[slug]}")
    for slug in sorted(stats.project_breakdown):
        console.print(f"    project={slug}:{' ' * (16 - len(slug))}{stats.project_breakdown[slug]}")
    console.print("  [bold]Planned rewrites (exact prefixes only)[/bold]")
    console.print(f"    mnemos:<subtype> -> vesma:<subtype>:  {stats.subtype_swaps}")
    console.print(f"    project:mnemos -> project:vesma:      {stats.project_swaps}")
    console.print(f"    duplicate tags removed:               {stats.duplicates_removed}")
    console.print(
        f"    trust markers kept byte-stable:       {stats.trust_markers_kept} (mnemos:no-federate)"
    )
    if plan.target == SYSTEM_TARGET and plan.fast_path:
        console.print(
            "    fast path: ZERO rewrites — the apply will be a pure "
            "backup-API copy (no UPDATE sweep)"
        )
    if plan.target == SYSTEM_TARGET:
        console.print(f"    source WAL size (advisory): {plan.wal_bytes} bytes")
    console.print("  [bold]Files[/bold]")
    for planned in plan.planned_files:
        via = "sqlite backup API" if planned.via_backup_api else "byte copy"
        console.print(
            f"    {planned.source} -> {planned.target} ({planned.size_bytes} bytes, {via})"
        )
    console.print(f"    vault present: {plan.vault_present}")
    console.print(f"    config present: {plan.config_present}")
    if plan.target == SYSTEM_TARGET:
        if plan.remaining_cargo:
            console.print("  [bold]NOT carried (the mover moves the store only)[/bold]")
            for line in plan.remaining_cargo:
                console.print(f"    - {line}")
        if plan.precondition_lines:
            console.print("  [bold]Preconditions (operator, before service start)[/bold]")
            for line in plan.precondition_lines:
                console.print(f"    ! {line}")
        for line in plan.secret_path_warnings:
            console.print(f"  [yellow]warning: {line}[/yellow]")
        for key_path in plan.config_drops:
            console.print(
                f"  [cyan]config: dropped user-path key: {key_path} "
                "(planned — journald is the primary sink; the value is "
                "not printed)[/cyan]"
            )
        for line in _forecast_lines():
            console.print(f"  [cyan]forecast: {line}[/cyan]")
    for warning in plan.quiesce_warnings:
        console.print(f"  [yellow]warning: {warning}[/yellow]")
    if plan.mode == "dry-run":
        if plan.target == SYSTEM_TARGET:
            console.print(
                "\n  dry-run: nothing was written. --apply for the system "
                "target is active since 6.5.0 (owner-ratified 2026-10-23 "
                "after the green round-trip soak, ADR-0045 Decision 9)."
            )
        else:
            console.print(
                "\n  dry-run: nothing was written. Re-run with [bold]--apply[/bold] to migrate."
            )


def _forecast_lines() -> list[str]:
    """The DR forecast (C13): the doctor's own check functions, rendered
    marked as a FORECAST — one implementation with `vesma doctor`."""
    from vesma.service.doctor_checks import system_target_forecast

    lines: list[str] = []
    for finding in system_target_forecast():
        line = f"{finding.check_id} [{finding.severity.value}]: {finding.detail}"
        if finding.fix_command:
            line += f" | fix: {finding.fix_command}"
        lines.append(line)
    return lines


def _print_report(report: MigrationReport) -> None:
    console.print("[bold green]migrate-store: applied and verified[/bold green]")
    console.print(f"  target:   {report.target_home}")
    console.print(f"  snapshot: {report.snapshot_dir} (rollback artifact)")
    console.print(f"  records:  {report.records}")
    console.print(f"  mnemos:<subtype> -> vesma:<subtype>:  {report.tag_subtypes_rewritten}")
    console.print(f"  project:mnemos -> project:vesma:      {report.project_slug_rewritten}")
    console.print(f"  duplicate tags removed:               {report.duplicates_removed}")
    console.print(f"  trust markers kept byte-stable:       {report.trust_markers_kept}")
    if report.target == SYSTEM_TARGET:
        console.print(f"  fast path (no UPDATE sweep): {report.fast_path}")
        console.print(f"  source WAL size (advisory): {report.wal_bytes} bytes")
    if report.config is not None:
        console.print("  [bold]Config[/bold]")
        if report.config.synthesized:
            console.print("    synthesized (no source config.yaml found)")
        else:
            console.print(f"    source: {report.config.source_config}")
        console.print(f"    section mnemos: -> vesma: renamed: {report.config.section_renamed}")
        console.print(f"    paths re-rooted: {report.config.paths_rerooted}")
        console.print(f"    db_name pinned to vesma.db: {report.config.db_name_pinned}")
    if report.target == SYSTEM_TARGET:
        console.print(f"    disposition: {report.config_disposition}")
        console.print(f"    final path:  {report.config_final_path}")
    if report.verification is not None:
        console.print("  [bold]Verification[/bold]")
        console.print("    record counts equal: True")
        console.print("    id-set digest equal: True")
        console.print(
            f"    sample field checksums: {report.verification['sample_size']} checked, "
            f"{report.verification['sample_mismatches']} mismatched"
        )
        console.print("    FTS rebuilt + integrity ok: True")
        console.print("    sqlite quick_check: ok on all moved databases")
        if report.target == SYSTEM_TARGET:
            console.print(f"    per-file sha256 parity: {report.verification.get('sha256_parity')}")
            console.print(
                f"    sibling per-table counts: {report.verification.get('sibling_table_parity')}"
            )
            console.print(f"    post-publish stat audit: {report.verification.get('stat_audit')}")
            console.print(
                "    owner/group applied (root run): "
                f"{report.verification.get('ownership_applied')}"
            )
    if report.target == SYSTEM_TARGET and report.remaining_cargo:
        console.print("  [bold]NOT carried (the mover moves the store only)[/bold]")
        for line in report.remaining_cargo:
            console.print(f"    - {line}")
    for warning in report.warnings:
        console.print(f"  [yellow]warning: {warning}[/yellow]")
    if report.target == SYSTEM_TARGET:
        console.print(
            "\n  [yellow]Note:[/yellow] the trust marker mnemos:no-federate "
            "stays byte-stable; the mover wrote NO installer artifacts and "
            "left the install-state untouched (an uninstall never silently "
            "deletes a migrated store). See ADR-0047."
        )
    else:
        console.print(
            "\n  [yellow]Note:[/yellow] the canonical tag namespace is vesma:* "
            "(legacy mnemos:* accepted on input; the mnemos:no-federate trust "
            "marker stays byte-stable). Point the service at the new home only "
            "together with this canonical-prefix code wave (see ADR-0044)."
        )


def _refuse_discovery() -> DiscoveryRefusedError:
    """Read-only discovery: suggest candidates, never proceed (items 5-6)."""
    candidates = discover_source_candidates()
    if len(candidates) == 1:
        return DiscoveryRefusedError(
            "--from is required (explicit-only contract). A single candidate "
            f"store home was found: {candidates[0]} — re-run with an explicit "
            "--from pointing at it. Nothing was touched."
        )
    if candidates:
        listed = "\n  ".join(str(c) for c in candidates)
        return DiscoveryRefusedError(
            "--from is required (explicit-only contract). Several candidate "
            f"store homes exist; pick ONE explicitly:\n  {listed}\n"
            "Nothing was touched."
        )
    return DiscoveryRefusedError(
        "--from is required (explicit-only contract). No known 5.x store home "
        "was found on this machine (~/.mnemos, ~/.local/share/vesma/core, "
        "~/.vesma). Nothing was touched."
    )


def _emit_progress(line: str) -> None:
    """C14 progress: plain stderr lines (paths and numbers only) — grep- and
    ssh-safe, no spinner, no cursor games."""
    print(f"migrate-store: {line}", file=sys.stderr, flush=True)


def migrate_store(
    from_home: _FROM_OPTION = None,
    to_home: _TO_OPTION = None,
    apply: _APPLY_OPTION = False,
    json_out: _JSON_OPTION = False,
    target: _TARGET_OPTION = None,
    snapshot_dir: _SNAPSHOT_DIR_OPTION = None,
) -> None:
    """Move a store: 5.x → 6.0 (explicit --to), or relocate into the
    installed system profile (--target=system, ADR-0047).

    Safety: explicit --from only; dry-run by default (read-only opens, the
    source's records stay unchanged); SQLite backup-API snapshot gate;
    quiesce gates; the mnemos:no-federate trust marker stays byte-stable;
    the report prints paths and numbers only. For --target=system the apply
    is active since 6.5.0 — owner-ratified 2026-10-23 after the green
    round-trip soak (ADR-0045 Decision 9); the dry-run (plan + DR forecast)
    stays fully functional. The legacy `vesma migrate` (ai-brain) command is
    unaffected.
    """
    try:
        if target is not None and target != SYSTEM_TARGET:
            raise UsageError(f"unknown --target {target!r} (only 'system' is defined)")
        system_mode = target == SYSTEM_TARGET
        if system_mode and to_home is not None:
            raise UsageError(
                "--to is not used with --target=system — the target resolves "
                "from the system install-state + the FHS manifest"
            )
        if system_mode and apply and not activation_gate_open():
            raise ActivationGateError(
                "--apply is GATED for --target=system (ADR-0045 Decision 9 / "
                "ADR-0047 C13): it refuses until the round-trip soak has "
                "closed green AND the owner has given the word. The dry-run "
                "(plan + DR forecast) is fully functional and is the soak's "
                "own tool"
            )
        if not system_mode and snapshot_dir is not None:
            raise UsageError("--snapshot-dir applies to --target=system only")
        if from_home is None:
            raise _refuse_discovery()
        if to_home is None and not system_mode:
            raise UsageError("--to is required (explicit target path)")
        plan = build_plan(
            from_home,
            to_home,
            apply=apply,
            target=SYSTEM_TARGET if system_mode else "user",
            snapshot_dir=snapshot_dir,
        )
        if not apply:
            if json_out:
                console.print_json(json.dumps(_plan_payload(plan)))
            else:
                _print_plan(plan)
            return
        report = run_migration(plan, progress=None if json_out else _emit_progress)
    except StoreMigrationError as exc:
        # Plain stderr write on purpose: no rich wrapping, so paths and
        # exit-code messages stay grep-able and machine-parseable.
        print(f"migrate-store: {exc}", file=sys.stderr)
        raise typer.Exit(code=exc.exit_code) from exc
    if json_out:
        console.print_json(json.dumps(_report_payload(report)))
    else:
        _print_report(report)


__all__ = ["migrate_store"]
