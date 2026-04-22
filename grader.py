#!/usr/bin/env python3
"""
Grader for argo-rollouts-canary-stuck task.

ALL checks are FUNCTIONAL end-to-end tests. Each tests ONE concern.

4 subscores, each weight 1/4:
1. template_rate_window_durable — Rate window >= 180s after 180s durability window
2. rollout_timeout_configured — progressDeadlineSeconds set AND no indefinite pause
3. canary_traffic_routing_correct — Canary service targetPort matches container port
4. rollout_healthy_clean_history — Rollout Healthy + Successful AR + no Failed/Inconclusive ARs
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


def get_rollout():
    rc, out, _ = run_cmd("kubectl get rollout bleater-like-service -n bleater -o json 2>/dev/null")
    if rc != 0 or not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


# ─────────────────────────────────────────────
# CHECK 1: template_rate_window_durable
# Tests ONE thing: is the rate() window >= 180s
# after the 180s durability window?
# Prompt: "scrape-interval changes...query parameters
# no longer match reality"
# ─────────────────────────────────────────────
def check_template_rate_window_durable(setup_info):
    """
    After 180s durability window, verify the AnalysisTemplate's
    rate() window is >= 180s (3x the 60s scrape interval).
    If the enforcer wasn't neutralized, the template will be reverted.
    """
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

    query = metrics[0].get("provider", {}).get("prometheus", {}).get("query", "")

    rate_matches = re.findall(r'rate\([^[]*\[(\d+)([smh])\]', query)
    if not rate_matches:
        return 0.0, "No rate() in query — template may have been reverted"

    value, unit = rate_matches[0]
    seconds = int(value) * {"s": 1, "m": 60, "h": 3600}.get(unit, 1)

    if seconds >= 180:
        return 1.0, f"Rate window {seconds}s >= 180s — durable and correct"
    else:
        return 0.0, f"Rate window {seconds}s < 180s (need >= 3x the 60s scrape interval)"


# ─────────────────────────────────────────────
# CHECK 2: rollout_timeout_configured
# Tests: progressDeadlineSeconds set AND no
# indefinite pause in canary steps.
# Prompt: "stalled promotions should time out and
# surface a clear condition"
# ─────────────────────────────────────────────
def check_rollout_timeout_configured(setup_info):
    """
    Verify the Rollout has timeout protection:
    1. progressDeadlineSeconds set (<= 1800)
    2. No indefinite pause: {} in canary steps
    Both are needed to prevent silent hangs.
    """
    rollout = get_rollout()
    if not rollout:
        return 0.0, "Rollout not found"

    issues = []

    # Check progressDeadlineSeconds
    pds = rollout.get("spec", {}).get("progressDeadlineSeconds")
    if pds is None or not isinstance(pds, int) or pds > 1800:
        issues.append(f"progressDeadlineSeconds missing or >1800 (got {pds})")

    # Check for indefinite pause
    steps = rollout.get("spec", {}).get("strategy", {}).get("canary", {}).get("steps", [])
    for i, step in enumerate(steps):
        if "pause" in step:
            pause = step["pause"]
            if not pause or pause.get("duration") is None:
                issues.append(f"Step {i} has indefinite pause (no duration)")

    if not issues:
        return 1.0, f"Timeout configured: progressDeadlineSeconds={pds}, all pauses have durations"
    else:
        return 0.0, f"Timeout issues: {'; '.join(issues)}"


# ─────────────────────────────────────────────
# CHECK 3: canary_traffic_routing_correct
# Tests: canary service targetPort matches the
# container's actual port.
# Prompt: "requests weren't routing to the canary
# as expected"
# ─────────────────────────────────────────────
def check_canary_traffic_routing_correct(setup_info):
    """
    Verify the canary Service targetPort matches the container port.
    Setup corrupts targetPort to 8099 — traffic goes nowhere.
    """
    rollout = get_rollout()
    if not rollout:
        return 0.0, "Rollout not found"

    canary_svc_name = rollout.get("spec", {}).get("strategy", {}).get(
        "canary", {}).get("canaryService", "")

    if not canary_svc_name:
        return 0.0, "No canaryService defined in Rollout"

    rc, svc_json, _ = run_cmd(
        f"kubectl get service {canary_svc_name} -n bleater -o json 2>/dev/null"
    )
    if rc != 0 or not svc_json:
        return 0.0, f"Canary service '{canary_svc_name}' not found"

    try:
        svc = json.loads(svc_json)
    except json.JSONDecodeError:
        return 0.0, "Failed to parse canary service"

    ports = svc.get("spec", {}).get("ports", [])
    if not ports:
        return 0.0, "Canary service has no ports"

    target_port = ports[0].get("targetPort", 0)

    if target_port == 8099:
        return 0.0, f"Canary targetPort still broken: {target_port}"

    # Check endpoints or match known port
    rc, endpoints, _ = run_cmd(
        f"kubectl get endpoints {canary_svc_name} -n bleater "
        f"-o jsonpath='{{.subsets[0].addresses}}' 2>/dev/null"
    )

    like_port = setup_info.get("LIKE_PORT", "8006")

    if endpoints and endpoints != "''" and endpoints != "null":
        return 1.0, f"Canary routes traffic (targetPort={target_port}, has endpoints)"

    if str(target_port) == str(like_port):
        return 1.0, f"Canary targetPort={target_port} matches container"

    return 0.0, f"Canary targetPort={target_port} but no endpoints"


# ─────────────────────────────────────────────
# CHECK 4: rollout_healthy_clean_history
# Tests: Rollout Healthy + Successful AR + no
# Failed/Error/Inconclusive ARs.
# Prompt: "rollout history looks messy...clean up"
# ─────────────────────────────────────────────
def check_rollout_healthy_clean_history(setup_info):
    """
    Rollout must be Healthy with a Successful AnalysisRun.
    No Failed, Error, or Inconclusive ARs may remain.
    """
    rollout = get_rollout()
    if not rollout:
        return 0.0, "Rollout not found"

    phase = rollout.get("status", {}).get("phase", "")

    if phase.lower() == "degraded":
        conditions = rollout.get("status", {}).get("conditions", [])
        return 0.0, f"Rollout Degraded: {[c.get('message', '') for c in conditions]}"

    if phase.lower() == "paused":
        return 0.0, "Rollout still Paused"

    # Get ALL AnalysisRuns
    rc, ar_json, _ = run_cmd("kubectl get analysisrun -n bleater -o json 2>/dev/null")
    ar_phases = []
    has_successful = False
    has_bad = False
    if ar_json:
        try:
            for ar in json.loads(ar_json).get("items", []):
                p = ar.get("status", {}).get("phase", "unknown")
                ar_phases.append(p)
                if p.lower() == "successful":
                    has_successful = True
                if p.lower() in ("failed", "error", "inconclusive"):
                    has_bad = True
        except json.JSONDecodeError:
            pass

    if phase.lower() == "healthy" and has_successful and not has_bad:
        return 1.0, "Clean: Healthy + Successful AR, no stale ARs"

    if phase.lower() == "healthy" and has_successful and has_bad:
        return 0.0, f"Healthy but stale ARs remain: {ar_phases}"

    if phase.lower() == "healthy" and not ar_phases:
        pds = rollout.get("spec", {}).get("progressDeadlineSeconds")
        if pds is not None:
            return 1.0, "Healthy, no ARs, progressDeadlineSeconds set"
        return 0.0, "Healthy but no ARs and no progressDeadlineSeconds"

    return 0.0, f"Not Healthy: phase={phase}, ARs={ar_phases}"


# ─────────────────────────────────────────────
# GRADE
# ─────────────────────────────────────────────
def grade(*args, **kwargs) -> GradingResult:
    os.environ["KUBECONFIG"] = "/etc/rancher/k3s/k3s.yaml"
    setup_info = load_setup_info()

    run_cmd("pkill -u ubuntu 2>/dev/null || true")

    print("[grader] Waiting 180s durability window...")
    time.sleep(180)

    checks = {
        "template_rate_window_durable": check_template_rate_window_durable,
        "rollout_timeout_configured": check_rollout_timeout_configured,
        "canary_traffic_routing_correct": check_canary_traffic_routing_correct,
        "rollout_healthy_clean_history": check_rollout_healthy_clean_history,
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
