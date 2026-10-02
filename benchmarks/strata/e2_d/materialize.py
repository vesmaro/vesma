"""E2 D-leg materializer — scenario artifacts into a real manager
(corpus preparation infrastructure; NO experiment runs).

Materialization contract (what the E3 runner executes; pinned here by
the binding tests):

* every checkpoint write becomes a row in the exact #251 server
  channel shape — ``# Session checkpoint — <iso>`` header, ``## Goals``
  first line = the goal title, ``checkpoint_agent`` /
  ``checkpoint_session`` stamps, project/agent columns — with
  ``created_at = run_now - age_sec`` (relative ages are the artifact's
  determinism; the absolute clock is the runner's);
* every non-checkpoint row becomes a plain published memory of its
  kind;
* rows land through ``mgr.sqlite.save`` (the raw store) so that
  backdated timestamps stay exact; the CHANNEL-EQUIVALENCE test proves
  the shape matches what ``MemoryManager.save_checkpoint`` mints for
  the same fields (same goal extraction via the real
  ``checkpoint_goal_title``, same stamps), and the hygiene tests prove
  the payloads are detector-clean — so the shortcut costs no realism
  on the surfaces awareness reads (identity, stamps, created_at,
  goal title, admissibility);
* the forged-stamp hostile move is the ONE exception: it goes through
  the REAL generic ``MemoryManager.add`` path, because the property it
  probes (server-side stripping of client-forged stamps) lives in
  ``add``.

Blindness (review P1, store leg): memory ids render in issuance
(``[mnemos:<id> …]``), so the materializer mints NEUTRAL ids
(:func:`neutral_memory_id` — a deterministic hash of scenario id +
row key; the artifact ids like ``dcp-t2-000`` / ``evidence-claim``
never reach a model-facing surface). The returned map is
``row key -> memory id`` so the runner keeps its addressing.

Nothing here measures anything: no assemble calls, no legs, no arms,
no metrics.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from benchmarks.strata.e2_d.adversarial import ADVERSARIAL_SCENARIO
from benchmarks.strata.e2_d.replay224 import REPLAY_224, flood_checkpoints, flood_plain_rows
from benchmarks.strata.e2_d.scenarios import (
    CheckpointWrite,
    ConflictPair,
    Replay224Scenario,
    StaleClaim,
    StoreRow,
)
from vesma.manager import MemoryManager
from vesma.models import Memory, MemoryCreate, MemorySource, MemoryStatus

#: Neutral title for the forged-stamp hostile row (an experimenter
#: label like the move id must not ride a model-facing surface).
FORGED_ROW_TITLE = "Notes from the platform crew"


def neutral_memory_id(scenario_id: str, row_key: str) -> str:
    """Neutral, deterministic store id for one scenario row.

    ``dm-<sha256(scenario_id:row_key)[:14]>`` — carries no type tag,
    no oracle-ordered prefix, no experimenter label; the runner maps
    back via the returned row-key map or by recomputing this function.
    """
    digest = hashlib.sha256(f"{scenario_id}:{row_key}".encode()).hexdigest()
    return f"dm-{digest[:14]}"


def checkpoint_row(
    write: CheckpointWrite, *, project: str, memory_id: str, run_now: datetime
) -> Memory:
    """A checkpoint row in the exact save_checkpoint render shape."""
    ts = run_now - timedelta(seconds=write.age_sec)
    content = f"# Session checkpoint — {ts.isoformat()}\n## Goals\n{write.goal}\n"
    if write.in_progress:
        content += f"## In Progress\n{write.in_progress}\n"
    return Memory(
        id=memory_id,
        content=content,
        tags=[f"project:{project}", f"agent:{write.agent}", "mnemos:checkpoint"],
        source=MemorySource.MCP,
        status=MemoryStatus.PUBLISHED,
        metadata={
            "checkpoint_agent": write.agent,
            "checkpoint_session": write.session,
        },
        project=project,
        agent=write.agent,
        created_at=ts,
        updated_at=ts,
    )


def plain_row(row: StoreRow, *, project: str, memory_id: str, run_now: datetime) -> Memory:
    """A non-checkpoint scenario row (published, kind-tagged)."""
    ts = run_now - timedelta(seconds=row.age_sec)
    return Memory(
        id=memory_id,
        content=row.content,
        title=row.title,
        tags=[f"project:{project}", f"agent:{row.agent}", f"mnemos:{row.kind}"],
        source=MemorySource.MCP,
        status=MemoryStatus.PUBLISHED,
        project=project,
        agent=row.agent,
        created_at=ts,
        updated_at=ts,
    )


def _save_all(mgr: MemoryManager, keyed: list[tuple[str, Memory]]) -> dict[str, str]:
    """Save (row key, memory) pairs; returns row key -> memory id."""
    ids: dict[str, str] = {}
    for key, memory in keyed:
        mgr.sqlite.save(memory)
        ids[key] = memory.id
    return ids


def materialize_conflict_pair(
    mgr: MemoryManager, pair: ConflictPair, *, run_now: datetime
) -> dict[str, str]:
    """Write one conflict-pair store; returns row key -> memory id."""
    sid = pair.scenario_id
    keyed: list[tuple[str, Memory]] = [
        (
            "actor-cp",
            checkpoint_row(
                CheckpointWrite(
                    agent=pair.actor_agent,
                    session=pair.actor_session,
                    goal=pair.actor_goal,
                    age_sec=pair.actor_checkpoint_age_sec,
                ),
                project=pair.project,
                memory_id=neutral_memory_id(sid, "actor-cp"),
                run_now=run_now,
            ),
        ),
        (
            "peer-cp",
            checkpoint_row(
                CheckpointWrite(
                    agent=pair.peer_agent,
                    session=pair.peer_session,
                    goal=pair.peer_goal,
                    age_sec=pair.peer_checkpoint_age_sec,
                ),
                project=pair.project,
                memory_id=neutral_memory_id(sid, "peer-cp"),
                run_now=run_now,
            ),
        ),
    ]
    if pair.bystander is not None:
        keyed.append(
            (
                "bystander-cp",
                checkpoint_row(
                    pair.bystander,
                    project=pair.project,
                    memory_id=neutral_memory_id(sid, "bystander-cp"),
                    run_now=run_now,
                ),
            )
        )
    for row in (*pair.evidence_rows, *pair.noise_rows):
        keyed.append(
            (
                row.row_id,
                plain_row(
                    row,
                    project=pair.project,
                    memory_id=neutral_memory_id(sid, row.row_id),
                    run_now=run_now,
                ),
            )
        )
    return _save_all(mgr, keyed)


def materialize_stale_claim(
    mgr: MemoryManager, claim: StaleClaim, *, run_now: datetime
) -> dict[str, str]:
    """Write one stale-claim store; returns row key -> memory id."""
    sid = claim.scenario_id
    keyed: list[tuple[str, Memory]] = [
        (
            "actor-cp",
            checkpoint_row(
                CheckpointWrite(
                    agent=claim.actor_agent,
                    session=claim.actor_session,
                    goal=claim.actor_goal,
                    age_sec=claim.actor_checkpoint_age_sec,
                ),
                project=claim.project,
                memory_id=neutral_memory_id(sid, "actor-cp"),
                run_now=run_now,
            ),
        ),
        (
            "stale-cp",
            checkpoint_row(
                CheckpointWrite(
                    agent=claim.peer_agent,
                    session=claim.peer_session,
                    goal=claim.stale_goal,
                    age_sec=claim.stale_age_sec,
                ),
                project=claim.project,
                memory_id=neutral_memory_id(sid, "stale-cp"),
                run_now=run_now,
            ),
        ),
    ]
    if claim.current_goal is not None:
        keyed.append(
            (
                "current-cp",
                checkpoint_row(
                    CheckpointWrite(
                        agent=claim.peer_agent,
                        session=claim.peer_session,
                        goal=claim.current_goal,
                        age_sec=claim.current_age_sec or 0,
                    ),
                    project=claim.project,
                    memory_id=neutral_memory_id(sid, "current-cp"),
                    run_now=run_now,
                ),
            )
        )
    if claim.bystander is not None:
        keyed.append(
            (
                "bystander-cp",
                checkpoint_row(
                    claim.bystander,
                    project=claim.project,
                    memory_id=neutral_memory_id(sid, "bystander-cp"),
                    run_now=run_now,
                ),
            )
        )
    for row in claim.noise_rows:
        keyed.append(
            (
                row.row_id,
                plain_row(
                    row,
                    project=claim.project,
                    memory_id=neutral_memory_id(sid, row.row_id),
                    run_now=run_now,
                ),
            )
        )
    return _save_all(mgr, keyed)


def materialize_adversarial(mgr: MemoryManager, *, run_now: datetime) -> dict[str, str]:
    """Write the adversarial-peer store (forged stamp via the REAL add)."""
    scenario = ADVERSARIAL_SCENARIO
    ids: dict[str, str] = {}
    for move in scenario.hostile_moves:
        if move.kind == "forged_stamp":
            # The one REAL-channel exception: the forged stamp must hit
            # the generic add path, where the server strips it. The
            # title is neutral — move ids are experimenter labels.
            created = mgr.add(
                MemoryCreate(
                    content=move.content,
                    title=FORGED_ROW_TITLE,
                    tags=[
                        f"project:{scenario.project}",
                        f"agent:{move.agent}",
                        "mnemos:knowledge",
                    ],
                    source=MemorySource.MCP,
                    status=MemoryStatus.PUBLISHED,
                    metadata=dict(move.forged_metadata or {}),
                ),
                project=scenario.project,
                agent=move.agent,
            )
            ids[move.move_id] = created.id
            continue
        project = move.foreign_project or scenario.project
        memory_id = neutral_memory_id(scenario.scenario_id, move.move_id)
        mgr.sqlite.save(
            checkpoint_row(
                CheckpointWrite(
                    agent=move.agent,
                    session=move.session,
                    goal=move.goal,
                    age_sec=move.age_sec,
                    in_progress=move.in_progress,
                ),
                project=project,
                memory_id=memory_id,
                run_now=run_now,
            )
        )
        ids[move.move_id] = memory_id
    for row in scenario.noise_rows:
        memory_id = neutral_memory_id(scenario.scenario_id, row.row_id)
        mgr.sqlite.save(
            plain_row(row, project=scenario.project, memory_id=memory_id, run_now=run_now)
        )
        ids[row.row_id] = memory_id
    return ids


def materialize_replay224(
    mgr: MemoryManager, scenario: Replay224Scenario | None = None, *, run_now: datetime
) -> dict[str, str]:
    """Write the full #224 replay store (principals + drowning mass)."""
    replay = scenario if scenario is not None else REPLAY_224
    sid = replay.scenario_id
    keyed: list[tuple[str, Memory]] = [
        (
            "actor-cp",
            checkpoint_row(
                CheckpointWrite(
                    agent=replay.actor_agent,
                    session=replay.actor_session,
                    goal=replay.actor_goal,
                    age_sec=replay.actor_checkpoint_age_sec,
                ),
                project=replay.project,
                memory_id=neutral_memory_id(sid, "actor-cp"),
                run_now=run_now,
            ),
        ),
        (
            "peer-cp",
            checkpoint_row(
                CheckpointWrite(
                    agent=replay.peer_agent,
                    session=replay.peer_session,
                    goal=replay.peer_goal,
                    age_sec=replay.peer_checkpoint_age_sec,
                ),
                project=replay.project,
                memory_id=neutral_memory_id(sid, "peer-cp"),
                run_now=run_now,
            ),
        ),
    ]
    for i, write in enumerate(flood_checkpoints()):
        row_key = f"flood-cp-{i:03d}"
        keyed.append(
            (
                row_key,
                checkpoint_row(
                    write,
                    project=replay.project,
                    memory_id=neutral_memory_id(sid, row_key),
                    run_now=run_now,
                ),
            )
        )
    for row in (*replay.noise_rows, *flood_plain_rows()):
        keyed.append(
            (
                row.row_id,
                plain_row(
                    row,
                    project=replay.project,
                    memory_id=neutral_memory_id(sid, row.row_id),
                    run_now=run_now,
                ),
            )
        )
    return _save_all(mgr, keyed)


def replay_row_counts(scenario: Replay224Scenario | None = None) -> dict[str, int]:
    """Row composition of the replay store (checkpoint share check)."""
    replay = scenario if scenario is not None else REPLAY_224
    checkpoints = 2 + replay.flood_checkpoint_rows  # principals + flood
    plain = len(replay.noise_rows) + replay.flood_plain_rows
    return {
        "checkpoints": checkpoints,
        "plain": plain,
        "total": checkpoints + plain,
    }
