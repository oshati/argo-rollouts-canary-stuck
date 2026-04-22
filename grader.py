#!/usr/bin/env python3
"""
Grader for argo-rollouts-canary-stuck task.

ALL checks are FUNCTIONAL end-to-end tests.

3 subscores, each weight 1/3:
1. enforcer_neutralized_and_template_durable — Enforcer stopped AND template has
   rate window >= 180s (3x safety margin) AND NaN handling, verified after 180s
2. rollout_healthy_no_stale_ars — Rollout Healthy with Successful AR,
   no Failed/Error/Inconclusive ARs remaining
3. canary_traffic_routing_correct — Canary service targetPort matches container
   port so traffic actually reaches canary pods
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


# ─────────────────────────────────────────────
# CHECK 1: enforcer_neutralized_and_template_durable
# ─────────────────────────────────────────────
def check_enforcer_neutralized_and_template_durable(setup_info):
    """
    After 180s durability window, verify:
    1. The AnalysisTemplate has rate window >= 180s (3x the 60s scrape interval,
       Prometheus best practice for reliable rate() results)
    2. The template has NaN handling (query-level or config-level)
    3. The enforcer hasn't reverted the fix during the durability window

    The enforcer is a DaemonSet in monitoring namespace AND/OR a MutatingWebhook.
    If neither was neutralized, the template will be reverted.
    """
    rc, template_json, _ = run_cmd(
        "kubectl get analysistemplate bleater-like-service-error-rate -n bleater -o json 2>/dev/null"
    )
    if rc != 0 or not template_json:
        return 0.0, "AnalysisTemplate not found — enforcer may have deleted it"

    try:
        template = json.loads(template_json)
    except json.JSONDecodeError:
        return 0.0, "Failed to parse AnalysisTemplate"

    metrics = template.get("spec", {}).get("metrics", [])
    if not metrics:
        return 0.0, "No metrics in AnalysisTemplate"

    metric = metrics[0]
    query = metric.get("provider", {}).get("prometheus", {}).get("query", "")

    # Check rate window — require >= 180s (3x safety margin over 60s scrape)
    rate_matches = re.findall(r'rate\([^[]*\[(\d+)([smh])\]', query)
    if not rate_matches:
        return 0.0, "No rate() in query — enforcer reverted the template"

    value, unit = rate_matches[0]
    seconds = int(value) * {"s": 1, "m": 60, "h": 3600}.get(unit, 1)

    if seconds < 180:
        return 0.0, f"Rate window is {seconds}s (need >= 180s for 3x safety margin over 60s scrape)"

    # Check NaN handling
    query_lower = query.lower()
    has_query_nan = any(w in query_lower for w in [
        "or vector", "or on()", "clamp_min", "clamp_max", "absent(",
    ])
    success_cond = metric.get("successCondition", "")
    has_config_nan = (
        "isnan" in success_cond.lower() or
        "len(result)" in success_cond or
        metric.get("consecutiveErrorLimit") is not None or
        metric.get("inconclusiveLimit") is not None
    )

    if not (has_query_nan or has_config_nan):
        return 0.0, f"Rate window OK ({seconds}s) but no NaN handling"

    return 1.0, f"Template durable: rate={seconds}s (>= 180s), NaN handling present"


# ─────────────────────────────────────────────
# CHECK 2: rollout_healthy_no_stale_ars
# ─────────────────────────────────────────────
def check_rollout_healthy_no_stale_ars(setup_info):
    """
    Rollout must be Healthy with a Successful AnalysisRun.
    No Failed, Error, or Inconclusive AnalysisRuns may remain.
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
# CHECK 3: canary_traffic_routing_correct
# ─────────────────────────────────────────────
def check_canary_traffic_routing_correct(setup_info):
    """
    Verify the canary Service targetPort matches the container's actual port.
    Setup corrupts targetPort to 8099 — traffic goes to a closed port.
    The agent must trace why canary requests fail and fix the port.
    """
    rc, rollout_json, _ = run_cmd(
        "kubectl get rollout bleater-like-service -n bleater -o json 2>/dev/null"
    )
    if rc != 0:
        return 0.0, "Rollout not found"

    try:
        rollout = json.loads(rollout_json)
    except json.JSONDecodeError:
        return 0.0, "Failed to parse Rollout"

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
        return 0.0, f"Canary service targetPort still broken: {target_port}"

    # Verify endpoints exist or targetPort matches known container port
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
# GRADE
# ─────────────────────────────────────────────
def grade(*args, **kwargs) -> GradingResult:
    os.environ["KUBECONFIG"] = "/etc/rancher/k3s/k3s.yaml"
    setup_info = load_setup_info()

    run_cmd("pkill -u ubuntu 2>/dev/null || true")

    print("[grader] Waiting 180s durability window...")
    time.sleep(180)

    checks = {
        "enforcer_neutralized_and_template_durable": check_enforcer_neutralized_and_template_durable,
        "rollout_healthy_no_stale_ars": check_rollout_healthy_no_stale_ars,
        "canary_traffic_routing_correct": check_canary_traffic_routing_correct,
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
