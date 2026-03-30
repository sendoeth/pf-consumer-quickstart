# pf-consumer-quickstart

End-to-end consumer verification pipeline for Post Fiat signal producers. Takes a fresh consumer from zero to a structured trust verdict in one command.

**One-paragraph promise:** Give this tool a producer ID and their artifacts (signals, proof surface, routing config), and it will chain discovery lookup, schema validation, routing audit, resolution recomputation, proof freshness verification, and a full trust audit into a single idempotent run — outputting a `consumer_verdict.json` with per-stage PASS/FAIL/WARN status and an overall trust grade you can act on.

## Quick Start

```bash
git clone https://github.com/sendoeth/pf-consumer-quickstart.git
cd pf-consumer-quickstart

# Run against included example
python quickstart.py --producer post-fiat-signals \
    --registry example_run/registry.json \
    --signals example_run/signals.json \
    --proof example_run/proof_surface.json \
    --prices example_run/prices.csv \
    --routing-config example_run/routing_policy.json \
    --regime SYSTEMIC --duration 19
```

Output:
```
============================================================
  Consumer Verdict: post-fiat-signals
============================================================
  Trust Score:   0.76
  Trust Grade:   B
  Verdict:       TRUST
============================================================
```

## Prerequisites

- Python 3.8+ (no external dependencies)
- Producer artifacts (any combination of: signal log, proof surface, routing policy, registry, price CSV)

## Pipeline Stages

| # | Stage | Companion Repo | Checks |
|---|-------|---------------|--------|
| 1 | Discovery | [pf-discovery-protocol](https://github.com/sendoeth/pf-discovery-protocol) | Producer identity, liveness, baseline reputation |
| 2 | Signal Fetch | [pf-signal-schema](https://github.com/sendoeth/pf-signal-schema) | Load signals from file, batch, or proof surface |
| 3 | Schema Validation | [pf-signal-schema](https://github.com/sendoeth/pf-signal-schema) | 9 required fields, conformance levels, INVERT semantics |
| 4 | Routing Audit | [pf-routing-protocol](https://github.com/sendoeth/pf-routing-protocol) | 5-gate pre-flight filter compliance |
| 5 | Resolution | [pf-resolution-protocol](https://github.com/sendoeth/pf-resolution-protocol) | Independent Brier decomposition, 7-component reputation |
| 6 | Proof | [pf-proof-protocol](https://github.com/sendoeth/pf-proof-protocol) / [pf-lifecycle-pipeline](https://github.com/sendoeth/pf-lifecycle-pipeline) | Freshness, CUSUM drift, rolling window accuracy |
| 7 | Full Audit | [pf-consumer-audit](https://github.com/sendoeth/pf-consumer-audit) | 7-dimension trust synthesis with hard gates |

Also references: [pf-aggregation-protocol](https://github.com/sendoeth/pf-aggregation-protocol) for multi-producer context.

## Usage

```bash
# Minimal — just proof surface (extracts signals automatically)
python quickstart.py --producer my-producer --proof proof_surface.json

# Full audit with all artifacts
python quickstart.py --producer my-producer \
    --registry registry.json \
    --signals signals.json \
    --proof proof_surface.json \
    --prices prices.csv \
    --routing-config routing_policy.json \
    --regime SYSTEMIC --duration 19 \
    -o consumer_verdict.json --json

# Auto-discover from directory
python quickstart.py --producer my-producer --artifacts-dir ./artifacts/

# HTTP registry
python quickstart.py --producer my-producer \
    --registry-url http://localhost:8090 --proof proof.json
```

## Output: consumer_verdict.json

```json
{
  "meta": {
    "quickstart_version": "1.0.0",
    "protocol_version": "1.0.0",
    "generated_at": "2026-03-30T09:17:05Z",
    "producer_id": "post-fiat-signals",
    "content_hash": "563be87535ef03de"
  },
  "verdict": {
    "trust_score": 0.76,
    "trust_grade": "B",
    "trust_verdict": "TRUST",
    "hard_gate_failures": []
  },
  "stages": [ ... ],
  "trust_components": { ... },
  "findings": [ ... ],
  "limitations": [ ... ]
}
```

## Trust Verdicts

| Score | Grade | Verdict | Action |
|-------|-------|---------|--------|
| >= 0.65 | B+ | TRUST | Safe to consume signals |
| >= 0.50 | C+ | TRUST_WITH_CAUTION | Consume with adjustments |
| >= 0.35 | D+ | INSUFFICIENT_EVIDENCE | Wait for more data |
| < 0.35 | F | DO_NOT_TRUST | Do not consume |

**Hard gates** (override trust score):
- Schema pass rate < 50% → DO_NOT_TRUST
- Drift status DEGRADED → DO_NOT_TRUST

## Tests

```bash
python -m pytest tests/ -v
```

112 tests covering stage chaining, partial failures, trust grade boundaries, malformed input, empty producers, and cross-schema compatibility with all 8 companion repos.

## Detailed Walkthrough

See [WALKTHROUGH.md](WALKTHROUGH.md) for a 1,700+ word step-by-step guide with annotated CLI commands, expected output, decision points, and troubleshooting.

## Example Run

The `example_run/` directory contains a complete worked example:
- `registry.json` — producer registry entry
- `signals.json` — 16 crypto signals (BTC/ETH/SOL/LINK) with regime context
- `proof_surface.json` — proof surface with rolling windows, drift, and reputation
- `routing_policy.json` — 5-gate routing config with SOL INVERT policy
- `prices.csv` — hourly prices for resolution
- `consumer_verdict.json` — generated verdict (TRUST, grade B, score 0.76)

## Protocol Version

1.0.0 — compatible with all companion protocol repos at v1.0.0.

## License

MIT
