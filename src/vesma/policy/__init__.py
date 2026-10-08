"""Policy engine and automation layer for Vesma. (M5)

Submodules:
  scheduler — APScheduler periodic tasks (cluster every 1h, synthesize every 6h, …)
  triggers  — Event-driven triggers: vault watcher debounce → batch pipeline
  engine    — Declarative YAML rule evaluation (~/.vesma/policies.yaml)
  dlq       — Dead letter queue for failed synthesis; CLI vesma dlq list/retry/discard

Idempotency key: hash(cluster_id, prompt_version, model_version) — v1 stand-in for
the deferred Cache Center (M11).
"""
