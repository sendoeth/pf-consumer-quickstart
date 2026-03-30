# Consumer Quickstart Walkthrough

A step-by-step guide for verifying a Post Fiat signal producer from zero. This walkthrough chains all eight companion protocols into a single verification pipeline, outputs a structured trust verdict, and explains every decision point along the way.

**Time to complete:** 10-15 minutes with artifacts in hand.

**Prerequisites:**
- Python 3.8+
- Producer artifacts: signal log, proof surface, and optionally a routing policy and registry
- Price data (CSV) if you want independent resolution verification

---

## Overview: The 7-Stage Pipeline

The quickstart pipeline runs seven verification stages in order:

```
Discovery → Signal Fetch → Schema Validation → Routing Audit →
Resolution Verification → Proof Verification → Full Audit
```

Each stage outputs PASS, FAIL, WARN, or SKIP. The final audit synthesizes all stages into a trust verdict: **TRUST**, **TRUST_WITH_CAUTION**, **INSUFFICIENT_EVIDENCE**, or **DO_NOT_TRUST**.

Two hard gates override everything:
1. **Schema failure** (pass rate < 50%) → automatic DO_NOT_TRUST
2. **Drift DEGRADED** → automatic DO_NOT_TRUST

---

## Stage 1: Discovery

**Purpose:** Look up the producer in a registry to verify identity, liveness, and baseline reputation.

**Companion repo:** [pf-discovery-protocol](https://github.com/sendoeth/pf-discovery-protocol)

```bash
python quickstart.py --producer post-fiat-signals \
    --registry example_run/registry.json \
    --signals example_run/signals.json \
    --proof example_run/proof_surface.json
```

**What it checks:**
- Producer exists in registry
- Liveness grade is ALIVE or DEGRADED (not UNRESPONSIVE)
- Reputation score >= 0.35
- At least one capability declared

**Expected output (discovery section):**
```json
{
  "stage": "discovery",
  "status": "PASS",
  "details": {
    "producer_id": "post-fiat-signals",
    "liveness_grade": "ALIVE",
    "reputation_score": 0.62,
    "reputation_grade": "B",
    "drift_status": "WATCH",
    "capabilities": ["crypto_signals", "regime_detection", ...]
  }
}
```

**Decision point:** If discovery returns FAIL, the producer is either unregistered or unresponsive. You can still proceed — the pipeline will skip discovery and assess based on artifacts alone — but the trust score will be lower.

**Troubleshooting:**
- "Producer not found" → verify the producer_id matches exactly (case-sensitive)
- If using `--registry-url`, ensure the registry server is running and accessible
- No registry at all? Use `--signals` and `--proof` directly — discovery will SKIP

---

## Stage 2: Signal Fetch

**Purpose:** Load producer signals from a file, batch, or proof surface.

**Companion repo:** [pf-signal-schema](https://github.com/sendoeth/pf-signal-schema) (defines the format)

The pipeline accepts multiple input formats:
- **Array of signals** (`[{signal}, {signal}, ...]`)
- **Batch object** (`{"signals": [...]}`)
- **Proof surface** with `resolved_signals` field

```bash
# From signal file
python quickstart.py --producer my-producer --signals signals.json --proof proof.json

# Signals extracted from proof surface automatically
python quickstart.py --producer my-producer --proof proof_surface.json
```

**Expected output:**
```json
{
  "stage": "signal_fetch",
  "status": "PASS",
  "details": {
    "n_signals": 16,
    "symbols": {"BTC": 4, "ETH": 4, "SOL": 4, "LINK": 4},
    "directions": {"bullish": 4, "bearish": 12},
    "actions": {"EXECUTE": 12, "WITHHOLD": 0, "INVERT": 4}
  }
}
```

**Decision point:** Check the `actions` breakdown. INVERT signals indicate weak-symbol handling is active — this is a good sign that the producer has a calibrated routing policy. A 100% EXECUTE rate with no WITHHOLD or INVERT may indicate a naive producer.

---

## Stage 3: Schema Validation

**Purpose:** Validate every signal against the canonical Post Fiat signal schema.

**Companion repo:** [pf-signal-schema](https://github.com/sendoeth/pf-signal-schema)

Every signal must have these 9 required fields:
`signal_id`, `producer_id`, `timestamp`, `symbol`, `direction`, `confidence`, `horizon_hours`, `action`, `schema_version`

The validator also checks:
- `confidence` in [0.0, 1.0]
- `direction` is "bullish" or "bearish"
- `action` is EXECUTE, WITHHOLD, or INVERT
- INVERT signals must include `weak_symbol` metadata
- `horizon_hours` in [1, 8760]

**Conformance levels:**
| Level | Fields Required |
|-------|----------------|
| MINIMAL | 9 core fields |
| STANDARD | + regime_context |
| FULL | + attribution_hash + calibration |

**Expected output:**
```json
{
  "stage": "schema_validation",
  "status": "PASS",
  "details": {
    "total_signals": 16,
    "passed": 16,
    "failed": 0,
    "pass_rate": 1.0,
    "level_distribution": {"STANDARD": 16}
  }
}
```

**Decision point:** A pass rate below 90% triggers WARN. Below 50% triggers the **schema hard gate** — automatic DO_NOT_TRUST regardless of other stages.

**Troubleshooting:**
- "Required field missing" → producer signals predate schema v1.0.0 or use a non-standard format
- "Must be EXECUTE, WITHHOLD, or INVERT" → producer is using custom action values
- "Required when action=INVERT" → producer declares INVERT but omits weak_symbol metadata

---

## Stage 4: Routing Audit

**Purpose:** Verify routing policy compliance — are signals being filtered correctly?

**Companion repo:** [pf-routing-protocol](https://github.com/sendoeth/pf-routing-protocol)

If you provide a routing config (`--routing-config`), the pipeline audits every signal against 5 gates:

1. **Regime gate** — is the current regime in the allowed list?
2. **Duration gate** — has the regime lasted long enough?
3. **Confidence gate** — does the signal meet minimum confidence?
4. **VOI gate** — is the expected value of information positive?
5. **Weak symbol gate** — is the symbol below the severity threshold?

```bash
python quickstart.py --producer post-fiat-signals \
    --signals signals.json --proof proof.json \
    --routing-config routing_policy.json \
    --regime SYSTEMIC --duration 19
```

**Expected output:**
```json
{
  "stage": "routing_audit",
  "status": "PASS",
  "details": {
    "total_input": 16,
    "total_emit": 8,
    "total_withhold": 8,
    "filter_rate": 0.5,
    "per_gate_pass_rates": {
      "regime_gate": 1.0,
      "duration_gate": 1.0,
      "confidence_gate": 1.0,
      "voi_gate": 0.5,
      "weak_symbol_gate": 0.75
    }
  }
}
```

**Decision point:** A filter rate of 0% with >10 signals may indicate no routing policy is active. A rate above 80% means the producer is withholding most signals — check whether regime conditions justify this (e.g., SYSTEMIC regime suppresses all signal types).

Without a routing config, the stage reports signal action fields only and returns WARN.

---

## Stage 5: Resolution Verification

**Purpose:** Independently recompute signal accuracy and Producer Reputation Score.

**Companion repo:** [pf-resolution-protocol](https://github.com/sendoeth/pf-resolution-protocol)

This stage resolves each signal against price data: was the predicted direction correct within the horizon window?

```bash
python quickstart.py --producer post-fiat-signals \
    --signals signals.json --proof proof.json \
    --prices prices.csv
```

**What it computes:**
- **Brier decomposition** (Murphy-Winkler): reliability, resolution, uncertainty
- **Binary accuracy** with Wilson 90% confidence intervals
- **Confidence-weighted accuracy** — rewards high-confidence correct signals
- **Per-symbol breakdown** — which symbols carry the edge?
- **7-component Reputation Score** — calibration quality, accuracy, abstention discipline, sharpness, karma validation, monotonicity

**Expected output (abbreviated):**
```json
{
  "stage": "resolution_verification",
  "status": "PASS",
  "details": {
    "n_resolved": 16,
    "overall": {
      "accuracy": {"point": 0.5625, "lower": 0.378, "upper": 0.733},
      "brier_score": 0.2357,
      "reliability": 0.0032,
      "cw_accuracy": 0.5714
    },
    "reputation": {
      "composite_score": 0.59,
      "grade": "C"
    }
  }
}
```

**Decision point:** Reputation below 0.35 (grade F) triggers FAIL. Accuracy below 45% triggers a warning — the producer is performing worse than random. Check `per_symbol` to identify which symbols are dragging performance.

Without price data, this stage returns WARN and skips resolution.

**Troubleshooting:**
- "No price data available" → provide `--prices` pointing to a CSV with columns: timestamp, symbol, price
- Low resolution rate → price data may not cover all signal horizons

---

## Stage 6: Proof Verification

**Purpose:** Verify the producer's proof surface is fresh, stable, and not drifting.

**Companion repos:**
- [pf-proof-protocol](https://github.com/sendoeth/pf-proof-protocol) — proof maintenance and CUSUM drift detection
- [pf-lifecycle-pipeline](https://github.com/sendoeth/pf-lifecycle-pipeline) — automated proof lifecycle

**What it checks:**
- **Freshness grade** — LIVE (<1h), RECENT (<24h), STALE (<72h), EXPIRED (>72h)
- **Drift status** — STABLE, WATCH, DRIFTING, DEGRADED
- **Resolved signal count** — minimum 10 for meaningful assessment
- **Reputation score** — minimum 0.35

```bash
python quickstart.py --producer post-fiat-signals --proof proof_surface.json
```

**Expected output:**
```json
{
  "stage": "proof_verification",
  "status": "PASS",
  "details": {
    "freshness_grade": "RECENT",
    "age_hours": 2.59,
    "drift_status": "WATCH",
    "n_resolved": 3440,
    "reputation_score": 0.489,
    "window_accuracies": {"7d": 0.5204, "14d": 0.5146, "30d": 0.4683}
  }
}
```

**Decision point:** DEGRADED drift is a **hard gate** — automatic DO_NOT_TRUST. The CUSUM detector has identified statistically significant accuracy decline. WATCH means the drift detector is tracking a potential decline but hasn't breached threshold yet.

STALE or EXPIRED freshness means the proof surface hasn't been updated recently — the producer may have gone offline or stopped resolving signals.

---

## Stage 7: Full Audit

**Purpose:** Synthesize all six preceding stages into a final trust verdict.

**Companion repo:** [pf-consumer-audit](https://github.com/sendoeth/pf-consumer-audit)

The audit computes a 7-dimension trust score:

| Component | Weight | Source |
|-----------|--------|--------|
| Schema compliance | 0.15 | Stage 3 pass rate |
| Freshness | 0.15 | Stage 6 freshness grade |
| Reputation | 0.20 | Stage 5 or 6 reputation score |
| Drift stability | 0.15 | Stage 6 drift status |
| Routing discipline | 0.10 | Stage 4 filter rate |
| Discovery health | 0.10 | Stage 1 status |
| Signal volume | 0.15 | Stage 2 signal count |

**Trust verdict thresholds:**
| Score | Grade | Verdict |
|-------|-------|---------|
| >= 0.65 | B+ | TRUST |
| >= 0.50 | C+ | TRUST_WITH_CAUTION |
| >= 0.35 | D+ | INSUFFICIENT_EVIDENCE |
| < 0.35 | F | DO_NOT_TRUST |

**Expected output:**
```
============================================================
  Consumer Verdict: post-fiat-signals
============================================================
  Trust Score:   0.76
  Trust Grade:   B
  Verdict:       TRUST
============================================================

  Trust Components:
  schema_compliance         [####################] 1.00 (w=0.15)
  freshness                 [#################...] 0.85 (w=0.15)
  reputation                [###########.........] 0.59 (w=0.20)
  drift_stability           [##############......] 0.70 (w=0.15)
  routing_discipline        [####################] 1.00 (w=0.10)
  discovery_health          [####################] 1.00 (w=0.10)
  signal_volume             [########............] 0.40 (w=0.15)
```

---

## Full Example: One Command

The quickstart supports auto-discovery from a directory:

```bash
# Place all artifacts in one directory
mkdir producer_artifacts/
cp proof_surface.json signals.json routing_policy.json prices.csv registry.json producer_artifacts/

# Run with auto-discovery
python quickstart.py --producer post-fiat-signals \
    --artifacts-dir producer_artifacts/ \
    --regime SYSTEMIC --duration 19 \
    -o consumer_verdict.json
```

Or specify everything explicitly:

```bash
python quickstart.py --producer post-fiat-signals \
    --registry registry.json \
    --signals signals.json \
    --proof proof_surface.json \
    --prices prices.csv \
    --routing-config routing_policy.json \
    --regime SYSTEMIC --duration 19 \
    -o consumer_verdict.json --json
```

Both produce `consumer_verdict.json` — the machine-consumable trust assessment.

---

## Interpreting the Verdict

The `consumer_verdict.json` output contains:

- **`verdict.trust_score`** — 0-1 composite trust score
- **`verdict.trust_grade`** — A/B/C/D/F letter grade
- **`verdict.trust_verdict`** — actionable verdict (TRUST/TRUST_WITH_CAUTION/INSUFFICIENT_EVIDENCE/DO_NOT_TRUST)
- **`verdict.hard_gate_failures`** — list of hard gate failures (empty if none)
- **`stages`** — per-stage PASS/FAIL/WARN/SKIP with detailed diagnostics
- **`trust_components`** — 7-dimension breakdown with scores and weights
- **`findings`** — all warnings and errors from all stages
- **`limitations`** — known biases and sample size caveats

**What to do with each verdict:**
- **TRUST** — producer meets all quality thresholds. Safe to consume signals.
- **TRUST_WITH_CAUTION** — some concerns (stale proof, low volume, missing routing). Consume with position sizing adjustments.
- **INSUFFICIENT_EVIDENCE** — not enough data to assess. Wait for more resolved signals.
- **DO_NOT_TRUST** — hard gate failure or critically low quality. Do not consume.

---

## Companion Repo Reference

| Stage | Repo | Purpose |
|-------|------|---------|
| 1 | [pf-discovery-protocol](https://github.com/sendoeth/pf-discovery-protocol) | Producer registry and liveness |
| 2-3 | [pf-signal-schema](https://github.com/sendoeth/pf-signal-schema) | Canonical signal format |
| 4 | [pf-routing-protocol](https://github.com/sendoeth/pf-routing-protocol) | Pre-flight routing gates |
| 5 | [pf-resolution-protocol](https://github.com/sendoeth/pf-resolution-protocol) | Signal resolution and reputation |
| 6 | [pf-proof-protocol](https://github.com/sendoeth/pf-proof-protocol) | Proof maintenance and drift |
| 6 | [pf-lifecycle-pipeline](https://github.com/sendoeth/pf-lifecycle-pipeline) | Automated proof lifecycle |
| 7 | [pf-consumer-audit](https://github.com/sendoeth/pf-consumer-audit) | Trust assessment |
| — | [pf-aggregation-protocol](https://github.com/sendoeth/pf-aggregation-protocol) | Multi-producer consensus |
