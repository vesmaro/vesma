"""``vesma migrate-store`` CLI — the B3 store mover 5.x → 6.0.

Thin wiring over :mod:`vesma.store_migration` (all semantics live there;
see that module's docstring for the normative safety contract).

Exit codes: 0 success · 2 usage error · 3 discovery refused (explicit
``--from`` required) · 4 quiesce violation · 5 snapshot failure ·
6 verification failure · 7 already migrated · 8 invalid source ·
9 config migration blocked.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from vesma.cli.util import console
from vesma.store_migration import (
    DiscoveryRefusedError,
    MigrationPlan,
    MigrationReport,
    StoreMigrationError,
    UsageError,
    build_plan,
    discover_source_candidates,
    run_migration,
)

_FROM_OPTION = Annotated[
    Path | None,
    typer.Option(
        "--from",
        help="REQUIRED explicit source store home (e.g. ~/.mnemos). "
        "Never guessed; omitted -> read-only discovery SUGGESTS candidates.",
    ),
]
_TO_OPTION = Annotated[
    Path | None,
    typer.Option(
        "--to",
        help="REQUIRED explicit target store home (6.0 layout). "
        "Must not exist at all (even an empty directory is refused — "
        "choose a fresh target); snapshot lands next to it.",
    ),
]
_APPLY_OPTION = Annotated[
    bool,
    typer.Option(
        "--apply",
        help="Write the migration. WITHOUT this flag the command is a "
        "dry-run: plan + counters, zero writes.",
    ),
]
_JSON_OPTION = Annotated[
    bool,
    typer.Option("--json", help="Machine-readable report (paths and numbers only)."),
]


def _plan_payload(plan: MigrationPlan) -> dict[str, Any]:
    return {
        "mode": plan.mode,
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


def _report_payload(report: MigrationReport) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mode": report.mode,
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
    return payload


def _print_plan(plan: MigrationPlan) -> None:
    stats = plan.stats
    console.print(f"[bold]migrate-store[/bold] — mode: [yellow]{plan.mode}[/yellow]")
    console.print(f"  from: {plan.source_home}")
    console.print(f"  to:   {plan.target_home}")
    console.print(f"  snapshot (on --apply): {plan.snapshot_dir}")
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
    console.print("  [bold]Files[/bold]")
    for planned in plan.planned_files:
        via = "sqlite backup API" if planned.via_backup_api else "byte copy"
        console.print(
            f"    {planned.source} -> {planned.target} ({planned.size_bytes} bytes, {via})"
        )
    console.print(f"    vault present: {plan.vault_present}")
    console.print(f"    config present: {plan.config_present}")
    for warning in plan.quiesce_warnings:
        console.print(f"  [yellow]warning: {warning}[/yellow]")
    if plan.mode == "dry-run":
        console.print(
            "\n  dry-run: nothing was written. Re-run with [bold]--apply[/bold] to migrate."
        )


def _print_report(report: MigrationReport) -> None:
    console.print("[bold green]migrate-store: applied and verified[/bold green]")
    console.print(f"  target:   {report.target_home}")
    console.print(f"  snapshot: {report.snapshot_dir} (rollback artifact)")
    console.print(f"  records:  {report.records}")
    console.print(f"  mnemos:<subtype> -> vesma:<subtype>:  {report.tag_subtypes_rewritten}")
    console.print(f"  project:mnemos -> project:vesma:      {report.project_slug_rewritten}")
    console.print(f"  duplicate tags removed:               {report.duplicates_removed}")
    console.print(f"  trust markers kept byte-stable:       {report.trust_markers_kept}")
    if report.config is not None:
        console.print("  [bold]Config[/bold]")
        if report.config.synthesized:
            console.print("    synthesized (no source config.yaml found)")
        else:
            console.print(f"    source: {report.config.source_config}")
        console.print(f"    section mnemos: -> vesma: renamed: {report.config.section_renamed}")
        console.print(f"    paths re-rooted: {report.config.paths_rerooted}")
        console.print(f"    db_name pinned to vesma.db: {report.config.db_name_pinned}")
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
    for warning in report.warnings:
        console.print(f"  [yellow]warning: {warning}[/yellow]")
    console.print(
        "\n  [yellow]Note:[/yellow] the 6.0 code at this release reads mnemos:* "
        "canonically; point the service at the new home only together with the "
        "canonical-prefix code wave (see ADR-0044)."
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


def migrate_store(
    from_home: _FROM_OPTION = None,
    to_home: _TO_OPTION = None,
    apply: _APPLY_OPTION = False,
    json_out: _JSON_OPTION = False,
) -> None:
    """Move a 5.x store to the 6.0 layout: vesma.db, vesma:* tags, vesma: config.

    Safety: explicit --from/--to only; dry-run by default (zero writes);
    SQLite backup-API snapshot gate; quiesce check; the mnemos:no-federate
    trust marker stays byte-stable; the report prints paths and numbers
    only. The legacy `vesma migrate` (ai-brain) command is unaffected.
    """
    try:
        if from_home is None:
            raise _refuse_discovery()
        if to_home is None:
            raise UsageError("--to is required (explicit target path)")
        plan = build_plan(from_home, to_home, apply=apply)
        if not apply:
            if json_out:
                console.print_json(json.dumps(_plan_payload(plan)))
            else:
                _print_plan(plan)
            return
        report = run_migration(plan)
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
