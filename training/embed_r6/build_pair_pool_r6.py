#!/usr/bin/env python3
"""Round-6 pair pool — round-5 recipe VERBATIM (single-line delta: OUT_DIR ->
runs/embed-r6). The corpus is the SAME sealed r5 corpus, so the pool must be
byte-identical to the round-5 pool; identity asserted after the build by
pool_key_list_sha256 equality vs wt/embed-r5-train runs (read-only source).

Prereg embed-round5-prereg.md §1.1/§3 + phase-0.5 mechanics (runlog 00:55):
the translation-contrastive term trains on the corpus's translation pairs.
Pool recipe = build_gate_set.py pairing machinery VERBATIM (as piloted in
make_pilot_subset.py, phase-0.5): real/llm via recomputed units + gen key
match; seed-dup inline via side_text_seed; twin inline; pair_key = hash of
both side hashes; construction-order dedup, first occurrence wins.

Round-5 eligibility (decisive run, differs from the 25% pilot subset only
in scope):
  * corpus = the sealed ROUND-5 corpus (fingerprint e64fdbea…, train 25420
    / val 1261) — the gate-set exclusions are ALREADY baked in; the gate
    manifest excluded_text_shas are re-asserted as ZERO hits (invariant,
    not a filter);
  * both sides in the r5 TRAIN slice under the expected source class;
  * one-pair-per-text;
  * + Tatoeba stratum: 750 inline pairs from r5_gate/tatoeba_notes.jsonl
    (sha 0be59a69…, CC-BY 2.0 FR), both sides asserted present in train
    under tatoeba-* classes;
  * translated-sibling is NEVER a positive (contrast pairs).

Also builds the sibling MONITOR population: the build_gate_baseline.py
80-pair sibling reconstruction against the round-4 corpus (the exact
population the production anchor median 0.6889 was measured on). Output
texts only; measured at gate time with the candidate (monitored, no
verdict). Sibling pairs are excluded from the training pool.

Read-only inputs; writes under training/runs/embed-r6/ (gitignored).
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

R5_WT = Path("/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/embed-r5-corpus")
R4_WT = Path("/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/embed-r4-corpus")
GEN_DIR = R4_WT / "data" / "embed-r4" / "gen"
SEED_DIR = R4_WT / "datasets" / "corpus-v43"
REAL_PART = R4_WT / "datasets" / "v43" / "real-part.jsonl"
GATE_MANIFEST = R4_WT / "data" / "embed-r4" / "r5_gate" / "manifest.json"
TATOEBA_NOTES = R4_WT / "data" / "embed-r4" / "r5_gate" / "tatoeba_notes.jsonl"

CORPUS_DIR = R5_WT / "data" / "embed-r5" / "corpus"
EXPECTED_FP = "e64fdbea65f0e6a4f2830a42ea6bd0d74a2974854d381427c4d5ba2ea4901c62"
EXPECTED_TATOEBA_SHA = "0be59a697b00e2637a94ccae2ff82543d09844e2b81414c40462588cd2cc7130"

OUT_DIR = Path(__file__).resolve().parents[1] / "runs" / "embed-r6"

sys.path.insert(0, str(R4_WT / "scripts"))
from gen_embed_r4_texts import (  # noqa: E402
    MAX_CHARS,
    UNIT_TOKEN_CAP,
    count_tokens,
    detect_lang,
    load_jsonl,
    normalise,
    text_hash,
)


def side_text_seed(side: dict) -> str:
    """Exact collect_seed composition (assemble_embed_r4.py)."""
    title = str(side.get("title") or "").strip()
    body = str(side.get("body") or "").strip()
    if not title and not body:
        return ""
    text = f"{title}. {body}" if title else body
    return normalise(text)[:MAX_CHARS]


def real_units() -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for rec in load_jsonl(REAL_PART):
        body = (rec.get("side") or {}).get("body") or rec.get("content") or ""
        if not isinstance(body, str) or not body.strip():
            continue
        key = rec.get("content_hash") or text_hash(body)
        src_lang = (rec.get("side") or {}).get("language") or rec.get("language") or detect_lang(body)
        target = "en" if src_lang == "ru" else "ru"
        unit = body.strip()
        while count_tokens(unit) > UNIT_TOKEN_CAP and "\n\n" in unit:
            unit = unit.rsplit("\n\n", 1)[0].strip()
        while count_tokens(unit) > UNIT_TOKEN_CAP and "\n" in unit:
            unit = unit.rsplit("\n", 1)[0].strip()
        if count_tokens(unit) > UNIT_TOKEN_CAP:
            while count_tokens(unit) > 350 and len(unit) > 400:
                unit = unit[: int(len(unit) * 0.8)].rsplit(" ", 1)[0].strip()
        out[key] = (unit, target)
    return out


def llm_units() -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    seen: set[str] = set()
    for path in sorted(GEN_DIR.glob("synth-mono.shard*.jsonl")):
        for row in load_jsonl(path):
            if row.get("status") != "ok":
                continue
            h = text_hash(row["text"])
            if h in seen:
                continue
            seen.add(h)
            target = "ru" if row["lang"] == "en" else "en"
            out[h] = (row["text"], target)
    return out


def load_r5_corpus() -> dict[str, dict]:
    """text_hash -> {file, line, source, lang, text}; asserts sealed fp."""
    tp, vp = CORPUS_DIR / "train.jsonl", CORPUS_DIR / "val.jsonl"
    digest = hashlib.sha256()
    digest.update(tp.read_bytes())
    digest.update(vp.read_bytes())
    if digest.hexdigest() != EXPECTED_FP:
        raise SystemExit(f"FINGERPRINT MISMATCH: {digest.hexdigest()} != {EXPECTED_FP}")
    corpus: dict[str, dict] = {}
    for fn in ("train.jsonl", "val.jsonl"):
        with open(CORPUS_DIR / fn, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                row = json.loads(line)
                h = text_hash(row["text"])
                if h in corpus:
                    raise SystemExit(f"corpus hash collision at {fn}:{i}")
                corpus[h] = {"file": fn, "line": i, "source": row["source"],
                             "lang": row["lang"], "text": row["text"]}
    return corpus


def main() -> int:
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    manifest = json.loads(GATE_MANIFEST.read_text())
    gate_shas = set(manifest["excluded_text_shas_all_sides"])
    assert manifest["integrity"]["n_text_shas"] == len(gate_shas)

    tat_sha = hashlib.sha256(TATOEBA_NOTES.read_bytes()).hexdigest()
    assert tat_sha == EXPECTED_TATOEBA_SHA, f"tatoeba sha mismatch: {tat_sha[:12]}"

    corpus = load_r5_corpus()
    units_real, units_llm = real_units(), llm_units()
    print(f"r5 corpus verified rows={len(corpus)}; real units={len(units_real)}; llm units={len(units_llm)}")

    # ---- candidates (verbatim recipe, construction order) ----
    candidates: list[dict] = []

    def add_candidate(cls, text_src, lang_src, text_dst, lang_dst):
        hs_src, hs_dst = text_hash(text_src), text_hash(text_dst)
        candidates.append({
            "class": cls,
            "sha_src": hs_src, "sha_dst": hs_dst,
            "text_src": corpus[hs_src]["text"] if hs_src in corpus else text_src,
            "lang_src": lang_src,
            "text_dst": corpus[hs_dst]["text"] if hs_dst in corpus else text_dst,
            "lang_dst": lang_dst,
            "row_src": corpus.get(hs_src),
            "row_dst": corpus.get(hs_dst),
            "pair_key": text_hash(f"{hs_src}\x00{hs_dst}"),
        })

    for path in sorted(SEED_DIR.glob("batch-translated-dup-*.jsonl")):
        for row in load_jsonl(path):
            rec, tr = row.get("record"), row.get("translation")
            if not isinstance(rec, dict) or not isinstance(tr, dict):
                continue
            t_rec, t_tr = side_text_seed(rec), side_text_seed(tr)
            if not t_rec or not t_tr:
                continue
            lang_rec = str(rec.get("lang") or rec.get("language") or "")
            lang_tr = str(tr.get("lang") or tr.get("language") or "")
            add_candidate("seed-dup", t_rec, lang_rec, t_tr, lang_tr)

    for path in sorted(GEN_DIR.glob("real-translations.shard*.jsonl")):
        for row in load_jsonl(path):
            if row.get("status") != "ok":
                continue
            unit = units_real.get(row.get("key"))
            if unit is None:
                continue
            unit_text, target = unit
            tr_text = normalise(row["text"])
            if not tr_text:
                continue
            add_candidate("real", unit_text, "ru" if target == "en" else "en", tr_text, target)

    for path in sorted(GEN_DIR.glob("llm-translations.shard*.jsonl")):
        for row in load_jsonl(path):
            if row.get("status") != "ok":
                continue
            unit = units_llm.get(row.get("key"))
            if unit is None:
                continue
            unit_text, target = unit
            tr_text = normalise(row["text"])
            if not tr_text:
                continue
            add_candidate("llm", unit_text, "ru" if target == "en" else "en", tr_text, target)

    for path in sorted(GEN_DIR.glob("synth-twins.shard*.jsonl")):
        for row in load_jsonl(path):
            if row.get("status") != "ok":
                continue
            ru_t, en_t = normalise(row["ru"]), normalise(row["en"])
            if not ru_t or not en_t:
                continue
            add_candidate("twin", ru_t, "ru", en_t, "en")

    seen: set[str] = set()
    deduped = []
    for c in candidates:
        if c["pair_key"] in seen:
            continue
        seen.add(c["pair_key"])
        deduped.append(c)
    print(f"candidates raw={len(candidates)} after pair_key dedup={len(deduped)}")

    # ---- eligibility on the r5 corpus ----
    expected_dst = {"seed-dup": "translated-dup", "real": "translated-real-r4",
                    "llm": "translated-llm-r4", "twin": "twin-"}
    expected_src = {"seed-dup": "translated-dup", "twin": "twin-"}
    stats: dict[str, int] = {}
    pool: list[dict] = []
    used_texts: dict[str, str] = {}
    for c in deduped:
        cls = c["class"]
        rs, rd = c["row_src"], c["row_dst"]
        if rs is None or rd is None:
            stats[f"{cls}:not-both-sides-in-corpus"] = stats.get(f"{cls}:not-both-sides-in-corpus", 0) + 1
            continue
        if rs["file"] != "train.jsonl" or rd["file"] != "train.jsonl":
            stats[f"{cls}:side-in-val-slice"] = stats.get(f"{cls}:side-in-val-slice", 0) + 1
            continue
        if not rd["source"].startswith(expected_dst[cls]):
            stats[f"{cls}:dst-wrong-class"] = stats.get(f"{cls}:dst-wrong-class", 0) + 1
            continue
        if cls in expected_src and not rs["source"].startswith(expected_src[cls]):
            stats[f"{cls}:src-wrong-class"] = stats.get(f"{cls}:src-wrong-class", 0) + 1
            continue
        gate_hits = {c["sha_src"], c["sha_dst"]} & gate_shas
        if gate_hits:
            # invariant: the sealed r5 corpus already excluded every gate sha
            raise SystemExit(f"INVARIANT VIOLATION: {cls} pair sides carry gate shas {gate_hits}")
        clash = None
        for h in (c["sha_src"], c["sha_dst"]):
            if h in used_texts:
                clash = used_texts[h]
        if clash:
            stats[f"{cls}:text-reused-in-{clash}"] = stats.get(f"{cls}:text-reused-in-{clash}", 0) + 1
            continue
        used_texts[c["sha_src"]] = cls
        used_texts[c["sha_dst"]] = cls
        pool.append(c)
        stats[f"{cls}:pool"] = stats.get(f"{cls}:pool", 0) + 1

    # ---- tatoeba stratum (inline pairs, both sides asserted in train) ----
    n_tat = 0
    for note in load_jsonl(TATOEBA_NOTES):
        ts, td = note["text_src"], note["text_dst"]
        assert text_hash(ts) == note["text_sha_src"] and text_hash(td) == note["text_sha_dst"]
        hs, hd = note["text_sha_src"], note["text_sha_dst"]
        rs, rd = corpus.get(hs), corpus.get(hd)
        if rs is None or rd is None:
            stats["tatoeba:side-not-in-corpus"] = stats.get("tatoeba:side-not-in-corpus", 0) + 1
            continue
        if rs["file"] != "train.jsonl" or rd["file"] != "train.jsonl":
            stats["tatoeba:side-in-val-slice"] = stats.get("tatoeba:side-in-val-slice", 0) + 1
            continue
        assert rs["source"] == note["source_class_src"] and rd["source"] == note["source_class_dst"], note["note_id"]
        if {hs, hd} & gate_shas:
            raise SystemExit(f"INVARIANT VIOLATION: tatoeba {note['note_id']} carries gate shas")
        clash = any(h in used_texts for h in (hs, hd))
        if clash:
            stats["tatoeba:text-reused"] = stats.get("tatoeba:text-reused", 0) + 1
            continue
        used_texts[hs] = "tatoeba"
        used_texts[hd] = "tatoeba"
        pool.append({
            "class": "tatoeba", "sha_src": hs, "sha_dst": hd,
            "text_src": rs["text"], "lang_src": note["lang_src"],
            "text_dst": rd["text"], "lang_dst": note["lang_dst"],
            "row_src": rs, "row_dst": rd,
            "pair_key": text_hash(f"{hs}\x00{hd}"),
        })
        n_tat += 1

    pool.sort(key=lambda c: c["pair_key"])
    by_cls: dict[str, int] = {}
    for c in pool:
        by_cls[c["class"]] = by_cls.get(c["class"], 0) + 1
    print(f"TRAIN POOL: {len(pool)} pairs by_class={by_cls}")

    out = {
        "kind": "embed-r5-contrastive-pair-pool",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "authority": ["docs/specs/embed-round5-prereg.md §1.1, §3", "ADR-0005",
                      "phase-0.5 runlog 00:55/02:06 (mechanics + lambda/tau)"],
        "corpus_fingerprint": EXPECTED_FP,
        "tatoeba_sha256": tat_sha,
        "counts": {
            "candidates_raw": len(candidates),
            "after_pair_key_dedup": len(deduped),
            "pool": len(pool),
            "by_class_pool": dict(sorted(by_cls.items())),
            "eligibility_stats": dict(sorted(stats.items())),
        },
        "pool_key_list_sha256": hashlib.sha256(
            "\n".join(c["pair_key"] for c in pool).encode()).hexdigest(),
        "pairs": [
            {"pair_key": c["pair_key"], "class": c["class"],
             "text_src": c["text_src"], "lang_src": c["lang_src"],
             "text_dst": c["text_dst"], "lang_dst": c["lang_dst"],
             "sha_src": c["sha_src"], "sha_dst": c["sha_dst"],
             "row_src": {k: c["row_src"][k] for k in ("file", "line", "source")},
             "row_dst": {k: c["row_dst"][k] for k in ("file", "line", "source")}}
            for c in pool
        ],
    }
    (OUT_DIR / "pairs_r5.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"-> {OUT_DIR / 'pairs_r5.json'}")

    # ---- sibling monitor population (build_gate_baseline recipe vs r4 corpus) ----
    # Exact population the production anchor median 0.6889 (n=80) was measured
    # on; measured at gate time with the candidate (monitored, no verdict).
    r4_corpus_path = R4_WT / "data" / "embed-r4" / "corpus"
    r4_fp = "7a3bf00b22951df59e3cb3e638c16e1c05f09a51223da8e19ef246ed29f8b645"
    digest = hashlib.sha256()
    digest.update((r4_corpus_path / "train.jsonl").read_bytes())
    digest.update((r4_corpus_path / "val.jsonl").read_bytes())
    assert digest.hexdigest() == r4_fp, "r4 corpus fp mismatch"
    r4_corpus: dict[str, str] = {}
    for fn in ("train.jsonl", "val.jsonl"):
        with open(r4_corpus_path / fn, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    row = json.loads(line)
                    r4_corpus[text_hash(row["text"])] = row["text"]

    sib_seen: set[str] = set()
    sibling: list[dict] = []
    for path in sorted(SEED_DIR.glob("batch-translated-sibling-*.jsonl")):
        for row in load_jsonl(path):
            rec, sib = row.get("record"), row.get("sibling")
            if not isinstance(rec, dict) or not isinstance(sib, dict):
                continue
            t_rec, t_sib = side_text_seed(rec), side_text_seed(sib)
            if not t_rec or not t_sib:
                continue
            key = hashlib.sha256(
                (hashlib.sha256(t_rec.encode()).hexdigest() + "\x00"
                 + hashlib.sha256(t_sib.encode()).hexdigest()).encode()
            ).hexdigest()
            if key in sib_seen:
                continue
            ha, hb = normalise(t_rec).lower(), normalise(t_sib).lower()
            ta, tb = r4_corpus.get(hashlib.sha256(ha.encode()).hexdigest()), \
                r4_corpus.get(hashlib.sha256(hb.encode()).hexdigest())
            if ta is None or tb is None:
                continue
            sib_seen.add(key)
            sibling.append({"key": key, "text_a": ta, "text_b": tb})
    # population parity check vs the recorded baseline (n=80)
    assert len(sibling) == 80, f"sibling population {len(sibling)} != baseline 80"
    sib_out = {
        "kind": "embed-r5-sibling-monitor-population",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": "build_gate_baseline.py sibling recipe verbatim vs the r4 corpus; "
                "candidate measured on these texts at gate time (monitored anchor, prod median 0.6889)",
        "n": len(sibling),
        "pairs": sibling,
    }
    (OUT_DIR / "sibling_pop.json").write_text(json.dumps(sib_out, indent=1, ensure_ascii=False))
    print(f"sibling monitor population: {len(sibling)} pairs -> {OUT_DIR / 'sibling_pop.json'}")
    print(f"done in {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
