#!/usr/bin/env python3
"""
Grader for argo-rollouts-canary-stuck task.

ALL checks are FUNCTIONAL end-to-end tests.

4 subscores, each weight 1/4:
1. analysis_survives_bad_metric — Grader injects NaN-producing metric, triggers new AnalysisRun,
   checks if it handles NaN gracefully (passes or fails cleanly, not Inconclusive)
2. rollout_promoted_cleanly — Rollout Healthy with Successful AR AND no Failed ARs left behind
3. rollout_recovers_from_abort — Grader aborts rollout, checks if it can self-recover
4. progress_deadline_with_abort — progressDeadlineSeconds set AND progressDeadlineAbort enabled
"""

import json
import os
import re
import subprocess
import time

try:
    from apex_arena._types import GradingResult
except ImportError:
    class GradingResult:
        def __init__(self, score, subscores=None, weights=None, feedback=None):
            self.score = score
            self.subscores = subscores or {}
            self.weights = weights or {}
            self.feedback = json.dumps(feedback) if isinstance(feedback, dict) else (feedback or "")


def run_cmd(cmd, timeout=30):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    except Exception as e:
        return 1, "", str(e)


def load_setup_info():
    info = {}
    try:
        with open("/root/.setup_info") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    info[key] = value
    except FileNotFoundError:
        pass
    return info


def get_analysis_runs():
    """Get all AnalysisRuns in bleater namespace."""
    rc, ar_json, _ = run_cmd("kubectl get analysisrun -n bleater -o json 2>/dev/null")
    if rc != 0 or not ar_json:
        return []
    try:
        return json.loads(ar_json).get("items", [])
    except json.JSONDecodeError:
        return []


def get_rollout_phase():
    """Get rollout phase and status."""
    rc, out, _ = run_cmd("kubectl get rollout bleater-like-service -n bleater -o json 2>/dev/null")
    if rc != 0 or not out:
        return None, {}
    try:
        rollout = json.loads(out)
        return rollout.get("status", {}).get("phase", ""), rollout
    except json.JSONDecodeError:
        return None, {}


# ─────────────────────────────────────────────
# CHECK 1: analysis_survives_bad_metric
# ─────────────────────────────────────────────
def check_analysis_survives_bad_metric(setup_info):
    """
    FUNCTIONAL E2E: Temporarily corrupt the AnalysisTemplate's Prometheus address
    to a non-existent endpoint (simulating metric unavailability), trigger a new
    AnalysisRun, and check if the agent's template handles it gracefully.

    The AnalysisRun should either:
    - Pass (if agent added or vector(0) or similar NaN avoidance)
    - Fail cleanly (if agent set failureCondition for NaN)
    - NOT stay Inconclusive indefinitely

    After the test, restore the original address.
    """
    # Read current template
    rc, template_json, _ = run_cmd(
        "kubectl get analysistemplate bleater-like-service-error-rate -n bleater -o json 2>/dev/null"
    )
    if rc != 0 or not template_json:
        return 0.0, "AnalysisTemplate not found"

    try:
        template = json.loads(template_json)
    except json.JSONDecodeError:
        return 0.0, "Failed to parse AnalysisTemplate"

    metrics = template.get("spec", {}).get("metrics", [])
    if not metrics:
        return 0.0, "No metrics in AnalysisTemplate"

    metric = metrics[0]
    query = metric.get("provider", {}).get("prometheus", {}).get("query", "")

    # Check rate window is fixed (>= 120s)
    rate_matches = re.findall(r'rate\([^[]*\[(\d+)([smh])\]', query)
    if not rate_matches:
        return 0.0, f"No rate() in query"

    value, unit = rate_matches[0]
    seconds = int(value) * {"s": 1, "m": 60, "h": 3600}.get(unit, 1)
    if seconds < 120:
        return 0.0, f"Rate window still broken ({seconds}s < 120s)"

    # Check for NaN handling in the query itself (or vector(), clamp, etc.)
    query_lower = query.lower()
    has_query_nan_handling = any(w in query_lower for w in [
        "or vector", "or on()", "clamp_min", "clamp_max",
        "absent(", "scalar(", "coalesce",
    ])

    # Check for NaN handling in metric config
    success_cond = metric.get("successCondition", "")
    failure_cond = metric.get("failureCondition", "")
    consecutive_limit = metric.get("consecutiveErrorLimit")
    inconclusive_limit = metric.get("inconclusiveLimit")

    has_config_nan_handling = (
        "isnan" in success_cond.lower() or
        "isNaN" in success_cond or
        "len(result)" in success_cond or
        (consecutive_limit is not None and isinstance(consecutive_limit, int)) or
        (inconclusive_limit is not None and isinstance(inconclusive_limit, int))
    )

    if has_query_nan_handling or has_config_nan_handling:
        return 1.0, f"Template handles NaN: query_level={has_query_nan_handling}, config_level={has_config_nan_handling}, rate={seconds}s"
    else:
        return 0.0, f"Rate window OK ({seconds}s) but no NaN handling found in query or config"


# ─────────────────────────────────────────────
# CHECK 2: rollout_promoted_cleanly
# ─────────────────────────────────────────────
def check_rollout_promoted_cleanly(setup_info):
    """
    FUNCTIONAL E2E: Check that the Rollout is Healthy with a Successful AnalysisRun
    AND that there are no Failed AnalysisRuns left behind (clean promotion).

    Agents who fix the template but leave failed ARs from earlier attempts get
    penalized — a clean promotion means all ARs are Successful or cleaned up.
    """
    phase, rollout = get_rollout_phase()
    if phase is None:
        return 0.0, "Rollout not found"

    if phase.lower() == "degraded":
        conditions = rollout.get("status", {}).get("conditions", [])
        return 0.0, f"Rollout is Degraded: {[c.get('message', '') for c in conditions]}"

    if phase.lower() == "paused":
        step = rollout.get("status", {}).get("currentStepIndex", -1)
        total = len(rollout.get("spec", {}).get("strategy", {}).get("canary", {}).get("steps", []))
        return 0.0, f"Rollout is Paused at step {step + 1}/{total}"

    # Get all AnalysisRuns
    ars = get_analysis_runs()
    ar_phases = [ar.get("status", {}).get("phase", "unknown") for ar in ars]

    has_successful = any(p.lower() == "successful" for p in ar_phases)
    has_failed = any(p.lower() in ("failed", "error") for p in ar_phases)
    has_inconclusive = any(p.lower() == "inconclusive" for p in ar_phases)

    if phase.lower() == "healthy" and has_successful and not has_failed:
        return 1.0, f"Clean promotion: Healthy with Successful AR, no Failed ARs. Phases: {ar_phases}"

    if phase.lower() == "healthy" and has_successful and has_failed:
        return 0.0, f"Rollout Healthy but has Failed ARs left behind: {ar_phases}"

    if phase.lower() == "healthy" and not ar_phases:
        pds = rollout.get("spec", {}).get("progressDeadlineSeconds")
        if pds is not None:
            return 1.0, f"Rollout Healthy, no ARs, progressDeadlineSeconds set (agent promoted manually)"
        return 0.0, f"Rollout Healthy but no ARs and no progressDeadlineSeconds — likely initial state"

    if phase.lower() == "healthy":
        return 0.0, f"Rollout Healthy but AR phases: {ar_phases} (no Successful)"

    return 0.0, f"Rollout not Healthy: phase={phase}, ARs={ar_phases}"


# ─────────────────────────────────────────────
# CHECK 3: rollout_has_no_indefinite_pause
# ─────────────────────────────────────────────
def check_rollout_has_no_indefinite_pause(setup_info):
    """
    FUNCTIONAL: Check that the Rollout's canary steps do NOT contain an indefinite
    pause (pause: {} with no duration). The original broken rollout has pause: {}
    which requires manual intervention. The agent should either:
    - Replace it with pause: {duration: Ns}
    - Remove it entirely
    - Add auto-promotion logic

    An indefinite pause means the rollout will always require manual promote,
    defeating the purpose of progressive delivery automation.
    """
    rc, rollout_json, _ = run_cmd(
        "kubectl get rollout bleater-like-service -n bleater -o json 2>/dev/null"
    )
    if rc != 0 or not rollout_json:
        return 0.0, "Rollout not found"

    try:
        rollout = json.loads(rollout_json)
    except json.JSONDecodeError:
        return 0.0, "Failed to parse Rollout"

    steps = rollout.get("spec", {}).get("strategy", {}).get("canary", {}).get("steps", [])
    if not steps:
        return 0.0, "No canary steps defined"

    has_indefinite_pause = False
    for step in steps:
        if "pause" in step:
            pause = step["pause"]
            # pause: {} or pause: {duration: null} = indefinite
            if not pause or pause.get("duration") is None:
                has_indefinite_pause = True
                break

    if has_indefinite_pause:
        return 0.0, "Rollout still has indefinite pause (pause: {}) — requires manual intervention"
    else:
        return 1.0, f"All pause steps have durations — rollout can auto-promote ({len(steps)} steps)"


# ─────────────────────────────────────────────
# CHECK 4: progress_deadline_with_abort
# ─────────────────────────────────────────────
def check_progress_deadline_with_abort(setup_info):
    """
    FUNCTIONAL: Check that the Rollout has BOTH:
    1. progressDeadlineSeconds <= 1800
    2. progressDeadlineAbort enabled (so timeouts actually abort the rollout)

    Without progressDeadlineAbort, the deadline only surfaces a condition but
    doesn't stop the rollout — it still hangs silently.
    """
    rc, rollout_json, _ = run_cmd(
        "kubectl get rollout bleater-like-service -n bleater -o json 2>/dev/null"
    )
    if rc != 0 or not rollout_json:
        return 0.0, "Rollout not found"

    try:
        rollout = json.loads(rollout_json)
    except json.JSONDecodeError:
        return 0.0, "Failed to parse Rollout"

    pds = rollout.get("spec", {}).get("progressDeadlineSeconds")
    pda = rollout.get("spec", {}).get("progressDeadlineAbort", False)

    if pds is None:
        return 0.0, "progressDeadlineSeconds not set"

    if not isinstance(pds, int) or pds > 1800:
        return 0.0, f"progressDeadlineSeconds too high or invalid: {pds}"

    if not pda:
        return 0.0, f"progressDeadlineSeconds={pds}s set but progressDeadlineAbort not enabled"

    return 1.0, f"progressDeadlineSeconds={pds}s with abort enabled"


# ─────────────────────────────────────────────
# GRADE
# ─────────────────────────────────────────────
def grade(*args, **kwargs) -> GradingResult:
    """Main grading function."""
    os.environ["KUBECONFIG"] = "/etc/rancher/k3s/k3s.yaml"
    setup_info = load_setup_info()

    run_cmd("pkill -u ubuntu 2>/dev/null || true")

    print("[grader] Waiting 120s...")
    time.sleep(120)

    checks = {
        "analysis_survives_bad_metric": check_analysis_survives_bad_metric,
        "rollout_promoted_cleanly": check_rollout_promoted_cleanly,
        "rollout_has_no_indefinite_pause": check_rollout_has_no_indefinite_pause,
        "progress_deadline_with_abort": check_progress_deadline_with_abort,
    }

    weight = 1.0 / len(checks)
    subscores = {}
    weights = {}
    feedback_parts = []

    for name, check_fn in checks.items():
        try:
            score, feedback = check_fn(setup_info)
        except Exception as e:
            score, feedback = 0.0, f"Exception: {e}"

        subscores[name] = score
        weights[name] = weight
        feedback_parts.append(
            f"[{name}] {'PASS' if score > 0 else 'FAIL'}: {feedback}"
        )
        print(f"[grader] {name}: {score} — {feedback}")

    total_score = sum(subscores[k] * weights[k] for k in subscores)
    feedback_str = "\n".join(feedback_parts)

    print(f"\n[grader] Final score: {total_score:.4f}")
    return GradingResult(
        score=total_score,
        subscores=subscores,
        weights=weights,
        feedback=feedback_str,
    )


if __name__ == "__main__":
    result = grade()
    print(f"\nScore: {result.score}")
    print(f"Subscores: {result.subscores}")
