#!/usr/bin/env python3
"""
Post Fiat Consumer Quickstart — End-to-End Producer Verification Pipeline

Single CLI entry point that chains 7 verification stages:
  1. Discovery    — look up producer in registry
  2. Signal Fetch — retrieve signal log from producer artifacts
  3. Schema       — validate signals against canonical schema
  4. Routing      — audit routing policy compliance
  5. Resolution   — recompute reputation from scratch
  6. Proof        — verify proof surface freshness and drift
  7. Audit        — run full 7-dimension trust assessment

Outputs consumer_verdict.json with per-stage PASS/FAIL/WARN and overall trust grade.

Usage:
    python quickstart.py --registry registry.json --producer post-fiat-signals
    python quickstart.py --proof proof.json --signals signals.json --prices prices.csv
    python quickstart.py --artifacts-dir ./producer_artifacts/ -o verdict.json
    python quickstart.py --registry-url http://localhost:8090 --producer my-producer --json

Companion repos:
    pf-discovery-protocol    https://github.com/sendoeth/pf-discovery-protocol
    pf-signal-schema         https://github.com/sendoeth/pf-signal-schema
    pf-routing-protocol      https://github.com/sendoeth/pf-routing-protocol
    pf-resolution-protocol   https://github.com/sendoeth/pf-resolution-protocol
    pf-proof-protocol        https://github.com/sendoeth/pf-proof-protocol
    pf-lifecycle-pipeline    https://github.com/sendoeth/pf-lifecycle-pipeline
    pf-consumer-audit        https://github.com/sendoeth/pf-consumer-audit
    pf-aggregation-protocol  https://github.com/sendoeth/pf-aggregation-protocol

Protocol version: 1.0.0
"""

import argparse
import json
import os
import sys
import hashlib
import copy
from datetime import datetime, timezone, timedelta
from pathlib import Path

__version__ = "1.0.0"

PROTOCOL_VERSION = "1.0.0"

# Grade thresholds
GRADE_THRESHOLDS = {"A": 0.80, "B": 0.65, "C": 0.50, "D": 0.35, "F": 0.0}
TRUST_VERDICTS = ["TRUST", "TRUST_WITH_CAUTION", "INSUFFICIENT_EVIDENCE", "DO_NOT_TRUST"]

# Stage names in pipeline order
STAGE_ORDER = [
    "discovery",
    "signal_fetch",
    "schema_validation",
    "routing_audit",
    "resolution_verification",
    "proof_verification",
    "full_audit",
]

# Companion repo URLs
COMPANION_REPOS = {
    "pf-discovery-protocol": "https://github.com/sendoeth/pf-discovery-protocol",
    "pf-signal-schema": "https://github.com/sendoeth/pf-signal-schema",
    "pf-routing-protocol": "https://github.com/sendoeth/pf-routing-protocol",
    "pf-resolution-protocol": "https://github.com/sendoeth/pf-resolution-protocol",
    "pf-proof-protocol": "https://github.com/sendoeth/pf-proof-protocol",
    "pf-lifecycle-pipeline": "https://github.com/sendoeth/pf-lifecycle-pipeline",
    "pf-consumer-audit": "https://github.com/sendoeth/pf-consumer-audit",
    "pf-aggregation-protocol": "https://github.com/sendoeth/pf-aggregation-protocol",
}


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _now_utc():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _grade_from_score(score):
    """Map 0-1 score to letter grade."""
    for grade, threshold in GRADE_THRESHOLDS.items():
        if score >= threshold:
            return grade
    return "F"


def _content_hash(data):
    """SHA-256 of JSON-serialized data for idempotency."""
    raw = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _safe_get(d, *keys, default=None):
    """Nested dict access with default."""
    current = d
    for k in keys:
        if isinstance(current, dict):
            current = current.get(k, default)
        else:
            return default
    return current


def _wilson_ci(successes, trials, z=1.645):
    """Wilson score 90% confidence interval."""
    if trials == 0:
        return {"point": 0.0, "lower": 0.0, "upper": 0.0, "n": 0}
    p = successes / trials
    denom = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denom
    spread = z * ((p * (1 - p) / trials + z * z / (4 * trials * trials)) ** 0.5) / denom
    return {
        "point": round(p, 6),
        "lower": round(max(0.0, center - spread), 6),
        "upper": round(min(1.0, center + spread), 6),
        "n": trials,
    }


# ---------------------------------------------------------------------------
# Stage result helpers
# ---------------------------------------------------------------------------

class StageResult:
    """Result of a single pipeline stage."""

    def __init__(self, stage_name, status="PENDING", details=None, findings=None):
        self.stage_name = stage_name
        self.status = status  # PASS, FAIL, WARN, SKIP, PENDING
        self.details = details or {}
        self.findings = findings or []
        self.started_at = None
        self.completed_at = None
        self.duration_ms = 0

    def start(self):
        self.started_at = _now_utc()

    def complete(self, status, details=None, findings=None):
        self.completed_at = _now_utc()
        self.status = status
        if details:
            self.details.update(details)
        if findings:
            self.findings.extend(findings)
        if self.started_at:
            delta = (self.completed_at - self.started_at).total_seconds()
            self.duration_ms = round(delta * 1000, 1)

    def to_dict(self):
        return {
            "stage": self.stage_name,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "details": self.details,
            "findings": self.findings,
        }


# ---------------------------------------------------------------------------
# Stage 1: Discovery
# ---------------------------------------------------------------------------

def run_discovery(producer_id, registry_data=None, registry_url=None):
    """Look up producer in registry. Returns (StageResult, producer_entry)."""
    result = StageResult("discovery")
    result.start()
    producer_entry = None

    if registry_url:
        # HTTP registry query
        try:
            import urllib.request
            url = f"{registry_url.rstrip('/')}/producers/{producer_id}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                producer_entry = json.loads(resp.read().decode())
        except Exception as e:
            result.complete("FAIL", details={
                "error": f"Registry HTTP query failed: {str(e)}",
                "registry_url": registry_url,
                "producer_id": producer_id,
            })
            return result, None

    elif registry_data:
        # Local registry file
        producers = registry_data.get("producers", [])
        if isinstance(registry_data, dict) and "producer_id" in registry_data:
            # Single producer entry
            if registry_data["producer_id"] == producer_id:
                producer_entry = registry_data
        else:
            for p in producers:
                if p.get("producer_id") == producer_id:
                    producer_entry = p
                    break
    else:
        result.complete("SKIP", details={
            "reason": "No registry provided, skipping discovery stage",
        })
        return result, None

    if producer_entry is None:
        result.complete("FAIL", details={
            "error": f"Producer '{producer_id}' not found in registry",
            "producer_id": producer_id,
        })
        return result, None

    # Validate producer entry fields
    checks = []
    liveness = _safe_get(producer_entry, "liveness", "liveness_grade", default="UNKNOWN")
    rep_score = _safe_get(producer_entry, "reputation_summary", "score", default=0.0)
    rep_grade = _safe_get(producer_entry, "reputation_summary", "grade", default="F")
    drift = _safe_get(producer_entry, "proof_summary", "drift_status", default="UNKNOWN")
    caps = producer_entry.get("capabilities", [])

    checks.append({"check": "producer_found", "passed": True})
    checks.append({"check": "liveness_grade", "value": liveness,
                    "passed": liveness in ("ALIVE", "DEGRADED")})
    checks.append({"check": "reputation_score", "value": rep_score,
                    "passed": rep_score >= 0.35})
    checks.append({"check": "capabilities_present", "value": len(caps),
                    "passed": len(caps) > 0})

    failed = [c for c in checks if not c["passed"]]
    status = "PASS" if len(failed) == 0 else ("WARN" if len(failed) <= 1 else "FAIL")

    findings = []
    if liveness == "UNRESPONSIVE":
        findings.append({
            "severity": "WARNING",
            "message": f"Producer liveness is UNRESPONSIVE",
        })
    if drift == "DEGRADED":
        findings.append({
            "severity": "WARNING",
            "message": f"Producer drift status is DEGRADED",
        })

    result.complete(status, details={
        "producer_id": producer_id,
        "liveness_grade": liveness,
        "reputation_score": rep_score,
        "reputation_grade": rep_grade,
        "drift_status": drift,
        "capabilities": caps,
        "checks": checks,
    }, findings=findings)

    return result, producer_entry


# ---------------------------------------------------------------------------
# Stage 2: Signal Fetch
# ---------------------------------------------------------------------------

def run_signal_fetch(signals_path=None, signals_data=None, proof_data=None):
    """Load signals from file, data, or extract from proof surface.
    Returns (StageResult, signals_list)."""
    result = StageResult("signal_fetch")
    result.start()
    signals = []

    if signals_data is not None:
        signals = signals_data if isinstance(signals_data, list) else []
    elif signals_path and os.path.exists(signals_path):
        try:
            with open(signals_path) as f:
                raw = json.load(f)
            if isinstance(raw, list):
                signals = raw
            elif isinstance(raw, dict):
                # Could be a batch or proof surface
                if "signals" in raw:
                    signals = raw["signals"]
                elif "resolved_signals" in raw:
                    signals = raw["resolved_signals"]
                else:
                    signals = [raw]
        except (json.JSONDecodeError, IOError) as e:
            result.complete("FAIL", details={
                "error": f"Failed to load signals: {str(e)}",
                "path": signals_path,
            })
            return result, []
    elif proof_data and "resolved_signals" in proof_data:
        signals = proof_data["resolved_signals"]
    else:
        result.complete("SKIP", details={
            "reason": "No signal source provided",
        })
        return result, []

    if len(signals) == 0:
        result.complete("WARN", details={
            "n_signals": 0,
            "warning": "No signals found in source",
        })
        return result, []

    # Basic signal inventory
    symbols = {}
    directions = {"bullish": 0, "bearish": 0}
    actions = {"EXECUTE": 0, "WITHHOLD": 0, "INVERT": 0}
    for s in signals:
        sym = s.get("symbol", "UNKNOWN")
        symbols[sym] = symbols.get(sym, 0) + 1
        d = s.get("direction", "")
        if d in directions:
            directions[d] += 1
        a = s.get("action", "EXECUTE")
        if a in actions:
            actions[a] += 1

    result.complete("PASS", details={
        "n_signals": len(signals),
        "symbols": symbols,
        "directions": directions,
        "actions": actions,
        "source": signals_path or "in_memory",
    })

    return result, signals


# ---------------------------------------------------------------------------
# Stage 3: Schema Validation
# ---------------------------------------------------------------------------

def _validate_signal_fields(signal):
    """Validate a single signal against required schema fields.
    Returns (is_valid, errors, conformance_level)."""
    required = ["signal_id", "producer_id", "timestamp", "symbol",
                 "direction", "confidence", "horizon_hours", "action",
                 "schema_version"]
    standard_fields = ["regime_context"]
    full_fields = ["attribution_hash", "calibration"]

    errors = []
    for field in required:
        if field not in signal:
            errors.append({"field": field, "message": f"Required field '{field}' missing"})

    # Type checks for present fields
    if "confidence" in signal:
        conf = signal["confidence"]
        if not isinstance(conf, (int, float)):
            errors.append({"field": "confidence", "message": "Must be numeric"})
        elif not (0.0 <= conf <= 1.0):
            errors.append({"field": "confidence", "message": "Must be in [0.0, 1.0]"})

    if "direction" in signal:
        if signal["direction"] not in ("bullish", "bearish"):
            errors.append({"field": "direction",
                           "message": "Must be 'bullish' or 'bearish'"})

    if "action" in signal:
        if signal["action"] not in ("EXECUTE", "WITHHOLD", "INVERT"):
            errors.append({"field": "action",
                           "message": "Must be EXECUTE, WITHHOLD, or INVERT"})
        if signal["action"] == "INVERT" and "weak_symbol" not in signal:
            errors.append({"field": "weak_symbol",
                           "message": "Required when action=INVERT"})

    if "horizon_hours" in signal:
        h = signal["horizon_hours"]
        if not isinstance(h, (int, float)):
            errors.append({"field": "horizon_hours", "message": "Must be numeric"})
        elif not (1 <= h <= 8760):
            errors.append({"field": "horizon_hours", "message": "Must be in [1, 8760]"})

    if "symbol" in signal:
        sym = signal.get("symbol", "")
        if not isinstance(sym, str) or len(sym) == 0 or len(sym) > 20:
            errors.append({"field": "symbol", "message": "Must be 1-20 char string"})

    # Determine conformance level
    if len(errors) > 0:
        level = "INCOMPLETE"
    elif all(f in signal for f in standard_fields):
        if all(f in signal for f in full_fields):
            level = "FULL"
        else:
            level = "STANDARD"
    else:
        level = "MINIMAL"

    return len(errors) == 0, errors, level


def run_schema_validation(signals):
    """Validate all signals against canonical schema.
    Returns (StageResult, conformance_report)."""
    result = StageResult("schema_validation")
    result.start()

    if not signals:
        result.complete("SKIP", details={"reason": "No signals to validate"})
        return result, {}

    total = len(signals)
    passed = 0
    failed = 0
    level_dist = {"MINIMAL": 0, "STANDARD": 0, "FULL": 0, "INCOMPLETE": 0}
    all_errors = []
    error_freq = {}

    for sig in signals:
        is_valid, errors, level = _validate_signal_fields(sig)
        level_dist[level] = level_dist.get(level, 0) + 1
        if is_valid:
            passed += 1
        else:
            failed += 1
            for err in errors:
                key = err["field"]
                error_freq[key] = error_freq.get(key, 0) + 1
            if len(all_errors) < 10:  # Cap stored errors
                all_errors.append({
                    "signal_id": sig.get("signal_id", "unknown"),
                    "errors": errors,
                })

    pass_rate = passed / total if total > 0 else 0.0
    conformance_report = {
        "total_signals": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": round(pass_rate, 4),
        "level_distribution": level_dist,
        "common_errors": sorted(
            [{"field": k, "count": v} for k, v in error_freq.items()],
            key=lambda x: -x["count"]
        )[:10],
    }

    if failed > 0:
        status = "FAIL" if pass_rate < 0.90 else "WARN"
        findings = [{
            "severity": "ERROR" if pass_rate < 0.90 else "WARNING",
            "message": f"{failed}/{total} signals failed schema validation",
        }]
    else:
        status = "PASS"
        findings = []

    # Check conformance level distribution
    if level_dist.get("FULL", 0) == total:
        findings.append({
            "severity": "INFO",
            "message": "All signals at FULL conformance level",
        })
    elif level_dist.get("STANDARD", 0) + level_dist.get("FULL", 0) == total:
        findings.append({
            "severity": "INFO",
            "message": "All signals at STANDARD or higher conformance",
        })

    result.complete(status, details=conformance_report, findings=findings)
    return result, conformance_report


# ---------------------------------------------------------------------------
# Stage 4: Routing Audit
# ---------------------------------------------------------------------------

def _compute_voi(accuracy, confidence):
    """Simplified VOI: E[karma|send] - E[karma|withhold]."""
    if accuracy <= 0 or accuracy >= 1:
        e_send = confidence * (2 * accuracy - 1)
        return {"e_karma_send": round(e_send, 6), "e_karma_withhold": 0.0,
                "voi": round(e_send, 6)}
    e_karma_send = confidence * (2 * accuracy - 1)
    e_karma_withhold = 0.0
    return {
        "e_karma_send": round(e_karma_send, 6),
        "e_karma_withhold": round(e_karma_withhold, 6),
        "voi": round(e_karma_send - e_karma_withhold, 6),
    }


def run_routing_audit(signals, routing_config=None, regime_id="UNKNOWN",
                      duration_days=0):
    """Audit routing policy compliance.
    Returns (StageResult, routing_report)."""
    result = StageResult("routing_audit")
    result.start()

    if not signals:
        result.complete("SKIP", details={"reason": "No signals to route"})
        return result, {}

    if routing_config is None:
        # No routing config — generate basic routing assessment
        n_execute = sum(1 for s in signals if s.get("action") == "EXECUTE")
        n_withhold = sum(1 for s in signals if s.get("action") == "WITHHOLD")
        n_invert = sum(1 for s in signals if s.get("action") == "INVERT")
        total = len(signals)
        filter_rate = (n_withhold + n_invert) / total if total > 0 else 0

        routing_report = {
            "total_input": total,
            "total_emit": n_execute,
            "total_withhold": n_withhold,
            "total_invert": n_invert,
            "filter_rate": round(filter_rate, 4),
            "config_provided": False,
            "regime_context": {
                "regime_id": regime_id,
                "duration_days": duration_days,
            },
        }

        findings = [{
            "severity": "INFO",
            "message": "No routing config provided; using signal action fields only",
        }]

        # Check for suspicious patterns
        if filter_rate == 0.0 and total > 10:
            findings.append({
                "severity": "WARNING",
                "message": "Zero filter rate with >10 signals may indicate missing routing policy",
            })

        result.complete("WARN", details=routing_report, findings=findings)
        return result, routing_report

    # With routing config — full audit
    gates = routing_config.get("gates", {})
    symbol_configs = routing_config.get("symbols", {})
    decisions = []
    gate_pass_counts = {}

    for sig in signals:
        sym = sig.get("symbol", "UNKNOWN")
        sym_config = symbol_configs.get(sym, {})
        conf = sig.get("confidence", 0.5)
        action = sig.get("action", "EXECUTE")
        gates_passed = []
        gates_failed = []

        # Regime gate
        if gates.get("regime_gate", {}).get("enabled", False):
            allowed = gates["regime_gate"].get("allowed_regimes", [])
            passed = regime_id in allowed or regime_id == "UNKNOWN"
            gate = {"gate_id": "regime_gate", "passed": passed,
                    "threshold": allowed, "actual": regime_id}
            (gates_passed if passed else gates_failed).append(gate)

        # Duration gate
        if gates.get("duration_gate", {}).get("enabled", False):
            min_days = sym_config.get("min_duration_days",
                                       gates["duration_gate"].get("default_min_days", 0))
            passed = duration_days >= min_days
            gate = {"gate_id": "duration_gate", "passed": passed,
                    "threshold": min_days, "actual": duration_days}
            (gates_passed if passed else gates_failed).append(gate)

        # Confidence gate
        if gates.get("confidence_gate", {}).get("enabled", False):
            min_conf = sym_config.get("min_confidence",
                                       gates["confidence_gate"].get("default_min_confidence", 0))
            passed = conf >= min_conf
            gate = {"gate_id": "confidence_gate", "passed": passed,
                    "threshold": min_conf, "actual": conf}
            (gates_passed if passed else gates_failed).append(gate)

        # VOI gate
        if gates.get("voi_gate", {}).get("enabled", False):
            accuracy = sym_config.get("accuracy", 0.50)
            min_voi = gates["voi_gate"].get("min_voi", 0.0)
            voi_data = _compute_voi(accuracy, conf)
            passed = voi_data["voi"] >= min_voi
            gate = {"gate_id": "voi_gate", "passed": passed,
                    "threshold": min_voi, "actual": voi_data["voi"]}
            (gates_passed if passed else gates_failed).append(gate)

        # Weak symbol gate
        if gates.get("weak_symbol_gate", {}).get("enabled", False):
            severity = sym_config.get("weakness_severity", "NONE")
            threshold = gates["weak_symbol_gate"].get("severity_threshold", "SEVERE")
            sev_order = {"NONE": 0, "MILD": 1, "MODERATE": 2, "SEVERE": 3}
            passed = sev_order.get(severity, 0) < sev_order.get(threshold, 3)
            gate = {"gate_id": "weak_symbol_gate", "passed": passed,
                    "threshold": threshold, "actual": severity}
            (gates_passed if passed else gates_failed).append(gate)

        # Determine action from gates
        if len(gates_failed) > 0:
            routed_action = "WITHHOLD"
        elif sym_config.get("weak_symbol_policy") == "INVERT":
            routed_action = "INVERT"
        else:
            routed_action = "EMIT"

        for g in gates_passed + gates_failed:
            gid = g["gate_id"]
            if gid not in gate_pass_counts:
                gate_pass_counts[gid] = {"passed": 0, "total": 0}
            gate_pass_counts[gid]["total"] += 1
            if g["passed"]:
                gate_pass_counts[gid]["passed"] += 1

        decisions.append({
            "signal_id": sig.get("signal_id", ""),
            "symbol": sym,
            "action": routed_action,
            "original_action": action,
            "gates_passed": gates_passed,
            "gates_failed": gates_failed,
        })

    n_emit = sum(1 for d in decisions if d["action"] == "EMIT")
    n_withhold = sum(1 for d in decisions if d["action"] == "WITHHOLD")
    n_invert = sum(1 for d in decisions if d["action"] == "INVERT")
    total = len(decisions)
    filter_rate = (n_withhold) / total if total > 0 else 0

    per_gate_pass_rates = {}
    for gid, counts in gate_pass_counts.items():
        per_gate_pass_rates[gid] = round(
            counts["passed"] / counts["total"], 4
        ) if counts["total"] > 0 else 0.0

    routing_report = {
        "total_input": total,
        "total_emit": n_emit,
        "total_withhold": n_withhold,
        "total_invert": n_invert,
        "filter_rate": round(filter_rate, 4),
        "invert_rate": round(n_invert / total, 4) if total > 0 else 0.0,
        "per_gate_pass_rates": per_gate_pass_rates,
        "config_provided": True,
        "regime_context": {
            "regime_id": regime_id,
            "duration_days": duration_days,
        },
        "n_decisions": len(decisions),
    }

    findings = []
    # Check for action mismatches (producer said EXECUTE but routing says WITHHOLD)
    mismatches = sum(1 for d in decisions
                     if d["action"] != d["original_action"]
                     and d["original_action"] != "EXECUTE")
    if mismatches > 0:
        findings.append({
            "severity": "INFO",
            "message": f"{mismatches} signals have action mismatch between producer and routing audit",
        })

    if filter_rate > 0.5:
        findings.append({
            "severity": "WARNING",
            "message": f"High filter rate ({filter_rate:.0%}) — most signals withheld",
        })

    status = "PASS"
    if filter_rate > 0.8:
        status = "WARN"

    result.complete(status, details=routing_report, findings=findings)
    return result, routing_report


# ---------------------------------------------------------------------------
# Stage 5: Resolution Verification
# ---------------------------------------------------------------------------

def _resolve_signal(signal, prices):
    """Resolve a single signal against price data.
    Returns resolved signal dict or None if no price data."""
    sym = signal.get("symbol", "")
    ts_str = signal.get("timestamp", "")
    horizon = signal.get("horizon_hours", 24)
    direction = signal.get("direction", "bullish")
    confidence = signal.get("confidence", 0.5)
    action = signal.get("action", "EXECUTE")

    try:
        if isinstance(ts_str, str):
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        else:
            ts = ts_str
    except (ValueError, TypeError):
        return None

    target_end = ts + timedelta(hours=horizon)

    # Find closest prices
    sym_prices = prices.get(sym, [])
    if not sym_prices:
        return None

    def find_closest(target, tolerance_hours=1.5):
        best = None
        best_delta = timedelta(hours=tolerance_hours)
        for pt, pp in sym_prices:
            delta = abs(pt - target)
            if delta < best_delta:
                best_delta = delta
                best = pp
        return best

    entry_price = find_closest(ts)
    exit_price = find_closest(target_end)

    if entry_price is None or exit_price is None:
        return None

    pct_change = (exit_price - entry_price) / entry_price
    actual_direction = "bullish" if pct_change >= 0 else "bearish"

    # Handle INVERT: effective direction is opposite of stated
    effective_direction = direction
    if action == "INVERT":
        effective_direction = "bearish" if direction == "bullish" else "bullish"

    outcome = 1.0 if actual_direction == effective_direction else 0.0
    brier = (confidence - outcome) ** 2
    karma = confidence * (2 * outcome - 1)

    resolved = dict(signal)
    resolved.update({
        "entry_price": round(entry_price, 4),
        "exit_price": round(exit_price, 4),
        "pct_change": round(pct_change, 6),
        "actual_direction": actual_direction,
        "effective_direction": effective_direction,
        "outcome": outcome,
        "brier_score": round(brier, 6),
        "karma": round(karma, 6),
    })
    return resolved


def _brier_decomposition(resolved_signals):
    """Murphy-Winkler Brier decomposition."""
    if not resolved_signals:
        return {}

    n = len(resolved_signals)
    confidences = [s["confidence"] for s in resolved_signals]
    outcomes = [s["outcome"] for s in resolved_signals]

    brier = sum((c - o) ** 2 for c, o in zip(confidences, outcomes)) / n
    base_rate = sum(outcomes) / n

    # Bin into 10 bins
    n_bins = min(10, n)
    bins = [[] for _ in range(n_bins)]
    for c, o in zip(confidences, outcomes):
        idx = min(int(c * n_bins), n_bins - 1)
        bins[idx].append((c, o))

    reliability = 0.0
    resolution = 0.0
    for b in bins:
        if not b:
            continue
        nk = len(b)
        fk = sum(c for c, _ in b) / nk
        ok = sum(o for _, o in b) / nk
        reliability += nk * (fk - ok) ** 2
        resolution += nk * (ok - base_rate) ** 2

    reliability /= n
    resolution /= n
    uncertainty = base_rate * (1 - base_rate)

    # Calibration slope via simple linear regression
    if n >= 2:
        mean_c = sum(confidences) / n
        mean_o = sum(outcomes) / n
        num = sum((c - mean_c) * (o - mean_o) for c, o in zip(confidences, outcomes))
        den = sum((c - mean_c) ** 2 for c in confidences)
        slope = num / den if den > 0 else 0.0
        intercept = mean_o - slope * mean_c
    else:
        slope = 0.0
        intercept = 0.0

    accuracy = sum(outcomes) / n
    cw_num = sum(c * o for c, o in zip(confidences, outcomes))
    cw_den = sum(confidences)
    cw_accuracy = cw_num / cw_den if cw_den > 0 else 0.0

    sharpness = sum((c - sum(confidences) / n) ** 2 for c in confidences) / n if n > 0 else 0.0

    return {
        "brier_score": round(brier, 6),
        "reliability": round(reliability, 6),
        "resolution": round(resolution, 6),
        "uncertainty": round(uncertainty, 6),
        "n": n,
        "base_rate": round(base_rate, 4),
        "accuracy": round(accuracy, 4),
        "cw_accuracy": round(cw_accuracy, 4),
        "calibration_slope": round(slope, 4),
        "calibration_intercept": round(intercept, 4),
        "sharpness": round(sharpness, 6),
    }


def _compute_reputation(decomp, resolved_signals):
    """Compute 7-component Producer Reputation Score."""
    if not decomp or decomp.get("n", 0) == 0:
        return {"composite_score": 0.0, "grade": "F", "components": {}}

    rel = decomp.get("reliability", 1.0)
    acc = decomp.get("accuracy", 0.0)
    cw_acc = decomp.get("cw_accuracy", 0.0)
    sharpness = decomp.get("sharpness", 0.0)

    # Component 1: Calibration quality (weight 0.20)
    cal_score = max(0.0, 1.0 - rel * 4)

    # Component 2: Binary accuracy (weight 0.15)
    acc_score = max(0.0, min(1.0, (acc - 0.50) / 0.20))

    # Component 3: CW accuracy (weight 0.20)
    cw_score = max(0.0, min(1.0, (cw_acc - 0.50) / 0.20))

    # Component 4: Abstention discipline (weight 0.10)
    n_withhold = sum(1 for s in resolved_signals if s.get("action") == "WITHHOLD")
    total = len(resolved_signals)
    abstention_score = n_withhold / total if total > 0 else 0.0

    # Component 5: Confidence sharpness (weight 0.10)
    sharp_score = max(0.0, min(1.0, sharpness / 0.05))

    # Component 6: Karma validation (weight 0.10)
    realized_karma = sum(s.get("karma", 0) for s in resolved_signals)
    predicted_karma = sum(
        s.get("confidence", 0.5) * (2 * s.get("confidence", 0.5) - 1)
        for s in resolved_signals
    )
    if abs(predicted_karma) > 0 and abs(realized_karma) > 0:
        ratio = min(abs(realized_karma) / abs(predicted_karma),
                    abs(predicted_karma) / abs(realized_karma))
        karma_score = ratio * 0.8 + 0.2
    else:
        karma_score = 0.5

    # Component 7: Monotonicity (weight 0.15)
    brackets = [(0, 0.30), (0.30, 0.50), (0.50, 0.60), (0.60, 1.01)]
    bracket_acc = []
    for lo, hi in brackets:
        bracket_sigs = [s for s in resolved_signals if lo <= s.get("confidence", 0) < hi]
        if bracket_sigs:
            bracket_acc.append(
                sum(s.get("outcome", 0) for s in bracket_sigs) / len(bracket_sigs)
            )
    monotonic = all(bracket_acc[i] <= bracket_acc[i + 1]
                     for i in range(len(bracket_acc) - 1)) if len(bracket_acc) >= 2 else True
    mono_score = 1.0 if monotonic else 0.0

    components = {
        "calibration_quality": {"score": round(cal_score, 4), "weight": 0.20},
        "binary_accuracy": {"score": round(acc_score, 4), "weight": 0.15},
        "cw_accuracy": {"score": round(cw_score, 4), "weight": 0.20},
        "abstention_discipline": {"score": round(abstention_score, 4), "weight": 0.10},
        "confidence_sharpness": {"score": round(sharp_score, 4), "weight": 0.10},
        "karma_validation": {"score": round(karma_score, 4), "weight": 0.10},
        "monotonicity": {"score": round(mono_score, 4), "weight": 0.15},
    }

    composite = sum(c["score"] * c["weight"] for c in components.values())
    grade = _grade_from_score(composite)

    return {
        "composite_score": round(composite, 4),
        "grade": grade,
        "components": components,
        "formula_version": "2.0.0",
    }


def _parse_prices(prices_path):
    """Parse CSV prices into {symbol: [(datetime, price)]}."""
    prices = {}
    try:
        with open(prices_path) as f:
            header = f.readline().strip().split(",")
            ts_idx = 0
            sym_idx = 1
            price_idx = 2
            for i, h in enumerate(header):
                h_lower = h.strip().lower()
                if h_lower == "timestamp":
                    ts_idx = i
                elif h_lower == "symbol":
                    sym_idx = i
                elif h_lower == "price":
                    price_idx = i

            for line in f:
                parts = line.strip().split(",")
                if len(parts) < 3:
                    continue
                try:
                    ts = datetime.fromisoformat(
                        parts[ts_idx].strip().replace("Z", "+00:00")
                    )
                    sym = parts[sym_idx].strip()
                    price = float(parts[price_idx].strip())
                    if sym not in prices:
                        prices[sym] = []
                    prices[sym].append((ts, price))
                except (ValueError, IndexError):
                    continue
    except (IOError, OSError):
        pass
    return prices


def run_resolution_verification(signals, prices_path=None, prices_data=None):
    """Recompute resolution and reputation from scratch.
    Returns (StageResult, resolution_report)."""
    result = StageResult("resolution_verification")
    result.start()

    if not signals:
        result.complete("SKIP", details={"reason": "No signals to resolve"})
        return result, {}

    # Load prices
    prices = {}
    if prices_data:
        prices = prices_data
    elif prices_path and os.path.exists(prices_path):
        prices = _parse_prices(prices_path)

    if not prices:
        result.complete("WARN", details={
            "reason": "No price data available — cannot verify resolution",
            "n_signals": len(signals),
        }, findings=[{
            "severity": "WARNING",
            "message": "Resolution verification skipped: no price data provided",
        }])
        return result, {}

    # Resolve signals
    resolved = []
    unresolved = []
    for sig in signals:
        r = _resolve_signal(sig, prices)
        if r:
            resolved.append(r)
        else:
            unresolved.append(sig)

    if not resolved:
        result.complete("WARN", details={
            "n_signals": len(signals),
            "n_resolved": 0,
            "n_unresolved": len(unresolved),
            "reason": "No signals could be resolved against price data",
        })
        return result, {}

    # Brier decomposition
    decomp = _brier_decomposition(resolved)

    # Per-symbol breakdown
    per_symbol = {}
    symbols = set(s.get("symbol", "") for s in resolved)
    for sym in symbols:
        sym_signals = [s for s in resolved if s.get("symbol") == sym]
        sym_decomp = _brier_decomposition(sym_signals)
        cum_karma = sum(s.get("karma", 0) for s in sym_signals)
        per_symbol[sym] = {
            "n": len(sym_signals),
            "accuracy": sym_decomp.get("accuracy", 0),
            "brier_score": sym_decomp.get("brier_score", 0),
            "cumulative_karma": round(cum_karma, 4),
            "mean_karma": round(cum_karma / len(sym_signals), 4) if sym_signals else 0,
        }

    # Reputation
    reputation = _compute_reputation(decomp, resolved)

    # Karma
    cum_karma = sum(s.get("karma", 0) for s in resolved)

    # Wilson CI on accuracy
    n_correct = sum(1 for s in resolved if s.get("outcome", 0) == 1.0)
    accuracy_ci = _wilson_ci(n_correct, len(resolved))

    resolution_report = {
        "n_signals": len(signals),
        "n_resolved": len(resolved),
        "n_unresolved": len(unresolved),
        "resolution_rate": round(len(resolved) / len(signals), 4),
        "overall": {
            "brier_score": decomp.get("brier_score", 0),
            "reliability": decomp.get("reliability", 0),
            "resolution": decomp.get("resolution", 0),
            "accuracy": accuracy_ci,
            "cw_accuracy": decomp.get("cw_accuracy", 0),
            "calibration_slope": decomp.get("calibration_slope", 0),
            "sharpness": decomp.get("sharpness", 0),
        },
        "per_symbol": per_symbol,
        "karma": {
            "cumulative": round(cum_karma, 4),
            "mean": round(cum_karma / len(resolved), 4),
        },
        "reputation": reputation,
    }

    # Assess
    findings = []
    rep_score = reputation.get("composite_score", 0)
    acc = accuracy_ci.get("point", 0)

    if acc < 0.45:
        findings.append({
            "severity": "WARNING",
            "message": f"Accuracy below random ({acc:.1%})",
        })
    if rep_score < 0.35:
        findings.append({
            "severity": "ERROR",
            "message": f"Reputation score critically low ({rep_score:.2f}, grade F)",
        })

    if len(resolved) < 30:
        findings.append({
            "severity": "INFO",
            "message": f"Small sample ({len(resolved)} resolved) — high variance in estimates",
        })

    status = "PASS"
    if rep_score < 0.35:
        status = "FAIL"
    elif rep_score < 0.50 or acc < 0.45:
        status = "WARN"

    result.complete(status, details=resolution_report, findings=findings)
    return result, resolution_report


# ---------------------------------------------------------------------------
# Stage 6: Proof Verification
# ---------------------------------------------------------------------------

def run_proof_verification(proof_data=None, proof_path=None, now=None):
    """Verify proof surface freshness and drift status.
    Returns (StageResult, proof_report)."""
    result = StageResult("proof_verification")
    result.start()

    if now is None:
        now = _now_utc()

    if proof_data is None and proof_path:
        try:
            with open(proof_path) as f:
                proof_data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            result.complete("FAIL", details={
                "error": f"Failed to load proof surface: {str(e)}",
            })
            return result, {}

    if proof_data is None:
        result.complete("SKIP", details={
            "reason": "No proof surface provided",
        })
        return result, {}

    # Extract key proof metrics
    freshness = proof_data.get("freshness", {})
    drift = proof_data.get("drift", {})
    snapshot = proof_data.get("snapshot", {})
    reputation = proof_data.get("reputation", {})
    meta = proof_data.get("meta", {})
    rolling = proof_data.get("rolling_windows", [])

    freshness_grade = freshness.get("freshness_grade", "UNKNOWN")
    age_hours = freshness.get("age_hours", 9999)
    drift_status = drift.get("drift_status", "UNKNOWN")
    n_resolved = snapshot.get("n_resolved_total", 0)
    rep_score = reputation.get("score", 0.0)
    rep_grade = reputation.get("grade", "F")

    # Compute freshness if we have last_resolved_at
    last_resolved = freshness.get("last_resolved_at")
    if last_resolved:
        try:
            last_dt = datetime.fromisoformat(str(last_resolved).replace("Z", "+00:00"))
            computed_age = (now - last_dt).total_seconds() / 3600
            age_hours = round(computed_age, 2)
            if age_hours <= 1:
                freshness_grade = "LIVE"
            elif age_hours <= 24:
                freshness_grade = "RECENT"
            elif age_hours <= 72:
                freshness_grade = "STALE"
            else:
                freshness_grade = "EXPIRED"
        except (ValueError, TypeError):
            pass

    # Rolling window analysis
    window_accuracies = {}
    for w in rolling:
        wid = w.get("window_id", w.get("window_days", ""))
        acc = w.get("accuracy", w.get("overall", {}).get("accuracy", None))
        if acc is not None:
            window_accuracies[str(wid)] = round(acc, 4) if isinstance(acc, float) else acc

    # CUSUM check
    cusum_pos = drift.get("cusum_pos", 0)
    cusum_neg = drift.get("cusum_neg", 0)

    checks = [
        {"check": "freshness_grade", "value": freshness_grade,
         "passed": freshness_grade in ("LIVE", "RECENT")},
        {"check": "drift_status", "value": drift_status,
         "passed": drift_status in ("STABLE", "WATCH")},
        {"check": "n_resolved", "value": n_resolved,
         "passed": n_resolved >= 10},
        {"check": "reputation_score", "value": rep_score,
         "passed": rep_score >= 0.35},
    ]

    proof_report = {
        "freshness_grade": freshness_grade,
        "age_hours": age_hours,
        "drift_status": drift_status,
        "cusum_pos": cusum_pos,
        "cusum_neg": cusum_neg,
        "n_resolved": n_resolved,
        "reputation_score": rep_score,
        "reputation_grade": rep_grade,
        "window_accuracies": window_accuracies,
        "checks": checks,
    }

    findings = []
    if freshness_grade in ("STALE", "EXPIRED"):
        findings.append({
            "severity": "WARNING",
            "message": f"Proof surface is {freshness_grade} ({age_hours:.1f}h old)",
        })
    if drift_status == "DEGRADED":
        findings.append({
            "severity": "ERROR",
            "message": "Proof drift status is DEGRADED — hard gate failure",
        })
    if drift_status == "DRIFTING":
        findings.append({
            "severity": "WARNING",
            "message": "Proof drift status is DRIFTING — accuracy declining",
        })

    n_failed = sum(1 for c in checks if not c["passed"])
    if drift_status == "DEGRADED":
        status = "FAIL"  # Hard gate
    elif n_failed >= 2:
        status = "FAIL"
    elif n_failed >= 1:
        status = "WARN"
    else:
        status = "PASS"

    result.complete(status, details=proof_report, findings=findings)
    return result, proof_report


# ---------------------------------------------------------------------------
# Stage 7: Full Audit
# ---------------------------------------------------------------------------

def _trust_component_scores(stage_results):
    """Compute 7-dimension trust components from stage results."""

    def _stage_status(name):
        for sr in stage_results:
            if sr.stage_name == name:
                return sr.status
        return "SKIP"

    def _stage_details(name):
        for sr in stage_results:
            if sr.stage_name == name:
                return sr.details
        return {}

    components = {}

    # 1. Schema compliance (weight 0.15)
    schema_status = _stage_status("schema_validation")
    schema_details = _stage_details("schema_validation")
    pass_rate = schema_details.get("pass_rate", 0)
    if schema_status == "PASS":
        schema_score = 1.0
    elif schema_status == "WARN":
        schema_score = max(0.3, pass_rate)
    elif schema_status == "SKIP":
        schema_score = 0.5
    else:
        schema_score = max(0.0, pass_rate * 0.5)
    components["schema_compliance"] = {"score": round(schema_score, 4), "weight": 0.15}

    # 2. Freshness (weight 0.15)
    proof_details = _stage_details("proof_verification")
    fg = proof_details.get("freshness_grade", "UNKNOWN")
    freshness_map = {"LIVE": 1.0, "RECENT": 0.85, "STALE": 0.4, "EXPIRED": 0.1, "UNKNOWN": 0.3}
    components["freshness"] = {
        "score": round(freshness_map.get(fg, 0.3), 4), "weight": 0.15
    }

    # 3. Reputation (weight 0.20)
    res_details = _stage_details("resolution_verification")
    rep = res_details.get("reputation", {})
    rep_score = rep.get("composite_score", 0)
    if not rep_score:
        # Fall back to proof reputation
        rep_score = proof_details.get("reputation_score", 0)
    components["reputation"] = {"score": round(min(1.0, rep_score), 4), "weight": 0.20}

    # 4. Drift stability (weight 0.15)
    drift = proof_details.get("drift_status", "UNKNOWN")
    drift_map = {"STABLE": 1.0, "WATCH": 0.7, "DRIFTING": 0.3, "DEGRADED": 0.0, "UNKNOWN": 0.3}
    components["drift_stability"] = {
        "score": round(drift_map.get(drift, 0.3), 4), "weight": 0.15
    }

    # 5. Routing discipline (weight 0.10)
    routing_details = _stage_details("routing_audit")
    filter_rate = routing_details.get("filter_rate", 0)
    config_present = routing_details.get("config_provided", False)
    if config_present:
        # Some filtering = discipline; zero or excessive = concerning
        if 0.05 <= filter_rate <= 0.50:
            routing_score = 1.0
        elif filter_rate == 0:
            routing_score = 0.6
        else:
            routing_score = 0.4
    else:
        routing_score = 0.5  # No config = neutral
    components["routing_discipline"] = {"score": round(routing_score, 4), "weight": 0.10}

    # 6. Discovery health (weight 0.10)
    disc_status = _stage_status("discovery")
    disc_details = _stage_details("discovery")
    if disc_status == "PASS":
        disc_score = 1.0
    elif disc_status == "WARN":
        disc_score = 0.6
    elif disc_status == "SKIP":
        disc_score = 0.5
    else:
        disc_score = 0.2
    components["discovery_health"] = {"score": round(disc_score, 4), "weight": 0.10}

    # 7. Signal volume (weight 0.15)
    fetch_details = _stage_details("signal_fetch")
    n_signals = fetch_details.get("n_signals", 0)
    if n_signals >= 100:
        vol_score = 1.0
    elif n_signals >= 50:
        vol_score = 0.8
    elif n_signals >= 20:
        vol_score = 0.6
    elif n_signals >= 5:
        vol_score = 0.4
    elif n_signals > 0:
        vol_score = 0.2
    else:
        vol_score = 0.0
    components["signal_volume"] = {"score": round(vol_score, 4), "weight": 0.15}

    return components


def run_full_audit(stage_results):
    """Synthesize all stage results into final trust assessment.
    Returns StageResult with trust grade."""
    result = StageResult("full_audit")
    result.start()

    components = _trust_component_scores(stage_results)
    composite = sum(c["score"] * c["weight"] for c in components.values())
    grade = _grade_from_score(composite)

    # Hard gates
    hard_gate_failures = []
    for sr in stage_results:
        if sr.stage_name == "schema_validation" and sr.status == "FAIL":
            schema_pr = sr.details.get("pass_rate", 0)
            if schema_pr < 0.50:
                hard_gate_failures.append("schema_failure")
        if sr.stage_name == "proof_verification":
            if sr.details.get("drift_status") == "DEGRADED":
                hard_gate_failures.append("drift_degraded")

    if hard_gate_failures:
        verdict = "DO_NOT_TRUST"
        composite = min(composite, 0.34)
        grade = "F"
    elif composite >= 0.65:
        verdict = "TRUST"
    elif composite >= 0.50:
        verdict = "TRUST_WITH_CAUTION"
    elif composite >= 0.35:
        verdict = "INSUFFICIENT_EVIDENCE"
    else:
        verdict = "DO_NOT_TRUST"

    # Collect all findings from all stages
    all_findings = []
    for sr in stage_results:
        for f in sr.findings:
            f_copy = dict(f)
            f_copy["stage"] = sr.stage_name
            all_findings.append(f_copy)

    # Summary counts
    n_pass = sum(1 for sr in stage_results if sr.status == "PASS")
    n_warn = sum(1 for sr in stage_results if sr.status == "WARN")
    n_fail = sum(1 for sr in stage_results if sr.status == "FAIL")
    n_skip = sum(1 for sr in stage_results if sr.status == "SKIP")

    audit_details = {
        "trust_score": round(composite, 4),
        "trust_grade": grade,
        "trust_verdict": verdict,
        "hard_gate_failures": hard_gate_failures,
        "components": components,
        "stage_summary": {
            "total": len(stage_results),
            "passed": n_pass,
            "warned": n_warn,
            "failed": n_fail,
            "skipped": n_skip,
        },
        "all_findings": all_findings,
    }

    if hard_gate_failures:
        status = "FAIL"
    elif n_fail > 0:
        status = "FAIL"
    elif n_warn >= 3:
        status = "WARN"
    elif n_warn > 0:
        status = "WARN" if composite < 0.65 else "PASS"
    else:
        status = "PASS"

    result.complete(status, details=audit_details)
    return result


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------

class ConsumerPipeline:
    """End-to-end consumer verification pipeline."""

    def __init__(self, producer_id="unknown", now=None):
        self.producer_id = producer_id
        self.now = now or _now_utc()
        self.stage_results = []
        self.signals = []
        self.resolved_signals = []

    def run(self, registry_data=None, registry_url=None,
            signals_path=None, signals_data=None,
            prices_path=None, prices_data=None,
            proof_path=None, proof_data=None,
            routing_config=None, routing_config_path=None,
            regime_id="UNKNOWN", duration_days=0):
        """Execute all 7 stages in order. Returns consumer_verdict dict."""

        # Load routing config if path provided
        if routing_config is None and routing_config_path:
            try:
                with open(routing_config_path) as f:
                    routing_config = json.load(f)
            except (json.JSONDecodeError, IOError):
                routing_config = None

        # Load proof data if path provided
        if proof_data is None and proof_path:
            try:
                with open(proof_path) as f:
                    proof_data = json.load(f)
            except (json.JSONDecodeError, IOError):
                proof_data = None

        # Stage 1: Discovery
        disc_result, producer_entry = run_discovery(
            self.producer_id, registry_data, registry_url
        )
        self.stage_results.append(disc_result)

        # Stage 2: Signal Fetch
        fetch_result, self.signals = run_signal_fetch(
            signals_path=signals_path,
            signals_data=signals_data,
            proof_data=proof_data,
        )
        self.stage_results.append(fetch_result)

        # Stage 3: Schema Validation
        schema_result, conformance = run_schema_validation(self.signals)
        self.stage_results.append(schema_result)

        # Stage 4: Routing Audit
        routing_result, routing_report = run_routing_audit(
            self.signals, routing_config, regime_id, duration_days
        )
        self.stage_results.append(routing_result)

        # Stage 5: Resolution Verification
        res_result, res_report = run_resolution_verification(
            self.signals, prices_path, prices_data
        )
        self.stage_results.append(res_result)

        # Stage 6: Proof Verification
        proof_result, proof_report = run_proof_verification(
            proof_data=proof_data, now=self.now
        )
        self.stage_results.append(proof_result)

        # Stage 7: Full Audit
        audit_result = run_full_audit(self.stage_results)
        self.stage_results.append(audit_result)

        return self._build_verdict()

    def _build_verdict(self):
        """Build the consumer_verdict.json output."""
        audit = self.stage_results[-1] if self.stage_results else None
        audit_details = audit.details if audit else {}

        verdict = {
            "meta": {
                "quickstart_version": __version__,
                "protocol_version": PROTOCOL_VERSION,
                "generated_at": _iso(self.now),
                "producer_id": self.producer_id,
                "content_hash": _content_hash({
                    "producer_id": self.producer_id,
                    "stages": [s.to_dict() for s in self.stage_results],
                }),
            },
            "verdict": {
                "trust_score": audit_details.get("trust_score", 0),
                "trust_grade": audit_details.get("trust_grade", "F"),
                "trust_verdict": audit_details.get("trust_verdict", "INSUFFICIENT_EVIDENCE"),
                "hard_gate_failures": audit_details.get("hard_gate_failures", []),
            },
            "stages": [sr.to_dict() for sr in self.stage_results],
            "trust_components": audit_details.get("components", {}),
            "findings": audit_details.get("all_findings", []),
            "summary": audit_details.get("stage_summary", {}),
            "companion_repos": COMPANION_REPOS,
            "limitations": self._compute_limitations(),
        }
        return verdict

    def _compute_limitations(self):
        """Generate limitations based on pipeline execution."""
        limitations = []

        # Check for skipped stages
        skipped = [sr.stage_name for sr in self.stage_results if sr.status == "SKIP"]
        if skipped:
            limitations.append({
                "id": "SKIPPED_STAGES",
                "description": f"Stages skipped: {', '.join(skipped)}. "
                               "Trust assessment incomplete.",
                "bias_direction": "OVERSTATED",
                "bias_magnitude": "Trust score may be higher than warranted "
                                  "due to missing verification data",
            })

        # Check resolution
        for sr in self.stage_results:
            if sr.stage_name == "resolution_verification":
                n_res = sr.details.get("n_resolved", 0)
                if 0 < n_res < 30:
                    limitations.append({
                        "id": "SMALL_SAMPLE",
                        "description": f"Only {n_res} resolved signals. "
                                       "Accuracy and reputation estimates have high variance.",
                        "bias_direction": "INDETERMINATE",
                        "bias_magnitude": f"Wilson 90% CI width ~{0.5/(n_res**0.5):.0%}",
                    })
                if sr.details.get("n_unresolved", 0) > 0:
                    unres = sr.details["n_unresolved"]
                    total = sr.details.get("n_signals", 1)
                    limitations.append({
                        "id": "RESOLUTION_GAP",
                        "description": f"{unres}/{total} signals unresolved "
                                       "due to missing price data.",
                        "bias_direction": "INDETERMINATE",
                        "bias_magnitude": "Unresolved signals excluded from all metrics",
                    })

        # Check proof freshness
        for sr in self.stage_results:
            if sr.stage_name == "proof_verification":
                fg = sr.details.get("freshness_grade", "UNKNOWN")
                if fg in ("STALE", "EXPIRED"):
                    limitations.append({
                        "id": "STALE_PROOF",
                        "description": f"Proof surface is {fg}. "
                                       "Recent performance may differ.",
                        "bias_direction": "UNDERSTATED",
                        "bias_magnitude": "Proof metrics may not reflect current producer quality",
                    })

        return limitations


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="Post Fiat Consumer Quickstart — End-to-End Producer Verification",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Verify producer from local artifacts
  python quickstart.py --producer post-fiat-signals \\
      --signals signals.json --proof proof.json --prices prices.csv

  # Verify via registry
  python quickstart.py --producer post-fiat-signals \\
      --registry registry.json --proof proof.json

  # Verify via HTTP registry
  python quickstart.py --producer post-fiat-signals \\
      --registry-url http://localhost:8090 --proof proof.json

  # Full audit with routing config
  python quickstart.py --producer post-fiat-signals \\
      --signals signals.json --proof proof.json --prices prices.csv \\
      --routing-config routing_policy.json --regime SYSTEMIC --duration 16

  # Artifacts directory (auto-discover files)
  python quickstart.py --producer post-fiat-signals \\
      --artifacts-dir ./producer_artifacts/

  # JSON output
  python quickstart.py --producer my-producer --proof proof.json --json
        """,
    )

    parser.add_argument("--producer", "-p", required=True,
                        help="Producer ID to verify")
    parser.add_argument("--registry", help="Path to registry.json")
    parser.add_argument("--registry-url", help="HTTP registry URL")
    parser.add_argument("--signals", help="Path to signals JSON file")
    parser.add_argument("--proof", help="Path to proof surface JSON")
    parser.add_argument("--prices", help="Path to prices CSV")
    parser.add_argument("--routing-config", help="Path to routing policy JSON")
    parser.add_argument("--artifacts-dir", help="Directory containing producer artifacts")
    parser.add_argument("--regime", default="UNKNOWN", help="Current regime ID")
    parser.add_argument("--duration", type=float, default=0,
                        help="Days in current regime")
    parser.add_argument("-o", "--output", help="Output path for consumer_verdict.json")
    parser.add_argument("--json", action="store_true", help="JSON output to stdout")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    return parser


def _auto_discover_artifacts(artifacts_dir):
    """Auto-discover producer artifacts from a directory."""
    found = {}
    if not os.path.isdir(artifacts_dir):
        return found

    for fname in os.listdir(artifacts_dir):
        fpath = os.path.join(artifacts_dir, fname)
        if not os.path.isfile(fpath):
            continue
        fl = fname.lower()
        if "proof" in fl and fl.endswith(".json"):
            found["proof"] = fpath
        elif "signal" in fl and fl.endswith(".json"):
            found["signals"] = fpath
        elif "routing" in fl and fl.endswith(".json"):
            found["routing_config"] = fpath
        elif "registry" in fl and fl.endswith(".json"):
            found["registry"] = fpath
        elif "price" in fl and fl.endswith(".csv"):
            found["prices"] = fpath
    return found


def _print_human_readable(verdict):
    """Print a human-readable summary."""
    v = verdict["verdict"]
    meta = verdict["meta"]

    print(f"\n{'='*60}")
    print(f"  Consumer Verdict: {meta['producer_id']}")
    print(f"{'='*60}")
    print(f"  Trust Score:   {v['trust_score']:.2f}")
    print(f"  Trust Grade:   {v['trust_grade']}")
    print(f"  Verdict:       {v['trust_verdict']}")
    if v.get("hard_gate_failures"):
        print(f"  Hard Gates:    {', '.join(v['hard_gate_failures'])}")
    print(f"{'='*60}")

    print(f"\n  Pipeline Stages:")
    print(f"  {'Stage':<28} {'Status':<8} {'Time'}")
    print(f"  {'-'*50}")
    for stage in verdict["stages"]:
        status_icon = {"PASS": "+", "FAIL": "X", "WARN": "!", "SKIP": "-"}.get(
            stage["status"], "?"
        )
        print(f"  [{status_icon}] {stage['stage']:<25} {stage['status']:<8} "
              f"{stage['duration_ms']:.0f}ms")

    # Trust components
    if verdict.get("trust_components"):
        print(f"\n  Trust Components:")
        for name, comp in verdict["trust_components"].items():
            bar_len = int(comp["score"] * 20)
            bar = "#" * bar_len + "." * (20 - bar_len)
            print(f"  {name:<25} [{bar}] {comp['score']:.2f} (w={comp['weight']:.2f})")

    # Findings
    findings = verdict.get("findings", [])
    if findings:
        print(f"\n  Findings ({len(findings)}):")
        for f in findings[:10]:
            sev = f.get("severity", "INFO")
            icon = {"ERROR": "X", "WARNING": "!", "INFO": "i"}.get(sev, "?")
            print(f"  [{icon}] [{f.get('stage', '')}] {f.get('message', '')}")

    # Limitations
    limitations = verdict.get("limitations", [])
    if limitations:
        print(f"\n  Limitations ({len(limitations)}):")
        for lim in limitations:
            print(f"  - {lim['id']}: {lim['description']}")

    print(f"\n  Generated: {meta['generated_at']}")
    print(f"  Content hash: {meta['content_hash']}")
    print()


def main():
    parser = build_parser()
    args = parser.parse_args()

    # Auto-discover artifacts from directory
    auto = {}
    if args.artifacts_dir:
        auto = _auto_discover_artifacts(args.artifacts_dir)

    # Resolve file paths (CLI args override auto-discovery)
    signals_path = args.signals or auto.get("signals")
    proof_path = args.proof or auto.get("proof")
    prices_path = args.prices or auto.get("prices")
    routing_path = args.routing_config or auto.get("routing_config")
    registry_path = args.registry or auto.get("registry")

    # Load registry
    registry_data = None
    if registry_path:
        try:
            with open(registry_path) as f:
                registry_data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Failed to load registry: {e}", file=sys.stderr)

    # Run pipeline
    pipeline = ConsumerPipeline(producer_id=args.producer)
    verdict = pipeline.run(
        registry_data=registry_data,
        registry_url=args.registry_url,
        signals_path=signals_path,
        prices_path=prices_path,
        proof_path=proof_path,
        routing_config_path=routing_path,
        regime_id=args.regime,
        duration_days=args.duration,
    )

    # Output
    if args.output:
        with open(args.output, "w") as f:
            json.dump(verdict, f, indent=2, default=str)
        print(f"Verdict written to {args.output}", file=sys.stderr)

    if args.json:
        print(json.dumps(verdict, indent=2, default=str))
    elif not args.output:
        _print_human_readable(verdict)
    elif not args.json:
        _print_human_readable(verdict)


if __name__ == "__main__":
    main()
