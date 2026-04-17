#!/usr/bin/env python3
"""
Grader for argo-rollouts-canary-stuck task.

ALL checks are FUNCTIONAL end-to-end tests.

4 subscores, each weight 1/4:
1. enforcer_stopped_and_template_fixed — DaemonSet enforcer neutralized, template durable
2. rollout_healthy_no_stale_ars — Healthy + Successful AR + NO Failed/Error/Inconclusive ARs
3. canary_service_correctly_configured — Canary service targetPort matches container AND Rollout references correct service
4. pods_stable_no_restarts — Pods running with 0 restarts, no liveness failures, progressDeadlineAbort set, no indefinite pause
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
# CHECK 1: enforcer_stopped_and_template_fixed
# The enforcer is a DaemonSet in monitoring ns,
# NOT a CronJob in bleater. Runs every 30s.
# After 180s durability window, template must still
# have the correct rate window + NaN handling.
# ─────────────────────────────────────────────
def check_enforcer_stopped_and_template_fixed(setup_info):
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

    rate_matches = re.findall(r'rate\([^[]*\[(\d+)([smh])\]', query)
    if not rate_matches:
        return 0.0, "No rate() in query — enforcer reverted the template"

    value, unit = rate_matches[0]
    seconds = int(value) * {"s": 1, "m": 60, "h": 3600}.get(unit, 1)

    if seconds < 120:
        return 0.0, f"Rate window is {seconds}s (< 120s) — enforcer reverted the template"

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

    return 1.0, f"Template durable: rate={seconds}s, NaN handling present"


# ─────────────────────────────────────────────
# CHECK 2: rollout_healthy_no_stale_ars
# Must be Healthy + Successful AR + NO Failed,
# Error, OR Inconclusive ARs. Stale ARs from
# setup must be cleaned up.
# ─────────────────────────────────────────────
def check_rollout_healthy_no_stale_ars(setup_info):
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
    has_bad = False  # Failed, Error, OR Inconclusive
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
        return 1.0, f"Clean: Healthy + Successful AR, no stale ARs"

    if phase.lower() == "healthy" and has_successful and has_bad:
        return 0.0, f"Healthy but stale ARs remain: {ar_phases}"

    if phase.lower() == "healthy" and not ar_phases:
        pds = rollout.get("spec", {}).get("progressDeadlineSeconds")
        if pds is not None:
            return 1.0, "Healthy, no ARs, progressDeadlineSeconds set"
        return 0.0, "Healthy but no ARs and no progressDeadlineSeconds"

    return 0.0, f"Not Healthy: phase={phase}, ARs={ar_phases}"


# ─────────────────────────────────────────────
# CHECK 3: canary_service_correctly_configured
# Must fix BOTH:
# 1. Canary service targetPort (8099 → real port)
# 2. Rollout canaryService field must reference a
#    service with correct targetPort
# ─────────────────────────────────────────────
def check_canary_service_correctly_configured(setup_info):
    rc, rollout_json, _ = run_cmd(
        "kubectl get rollout bleater-like-service -n bleater -o json 2>/dev/null"
    )
    if rc != 0:
        return 0.0, "Rollout not found"

    try:
        rollout = json.loads(rollout_json)
    except json.JSONDecodeError:
        return 0.0, "Failed to parse Rollout"

    # Get the canaryService name from the Rollout
    canary_svc_name = rollout.get("spec", {}).get("strategy", {}).get(
        "canary", {}).get("canaryService", "")

    if not canary_svc_name:
        return 0.0, "No canaryService defined in Rollout"

    # Get the actual service
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

    # The broken targetPort is 8099
    if target_port == 8099:
        return 0.0, f"Canary service '{canary_svc_name}' targetPort still broken: {target_port}"

    # Verify the service has endpoints
    rc, endpoints, _ = run_cmd(
        f"kubectl get endpoints {canary_svc_name} -n bleater "
        f"-o jsonpath='{{.subsets[0].addresses}}' 2>/dev/null"
    )

    like_port = setup_info.get("LIKE_PORT", "8006")

    if endpoints and endpoints != "''" and endpoints != "null":
        return 1.0, f"Canary '{canary_svc_name}' routes traffic (targetPort={target_port})"

    if str(target_port) == str(like_port):
        return 1.0, f"Canary '{canary_svc_name}' targetPort={target_port} matches container"

    return 0.0, f"Canary '{canary_svc_name}' targetPort={target_port} but no endpoints"


# ─────────────────────────────────────────────
# CHECK 4: pods_stable_no_restarts
# Pods must be Running with 0 restarts (catches
# the delayed liveness probe), plus all config checks.
# ─────────────────────────────────────────────
def check_pods_stable_no_restarts(setup_info):
    rc, rollout_json, _ = run_cmd(
        "kubectl get rollout bleater-like-service -n bleater -o json 2>/dev/null"
    )
    if rc != 0 or not rollout_json:
        return 0.0, "Rollout not found"

    try:
        rollout = json.loads(rollout_json)
    except json.JSONDecodeError:
        return 0.0, "Failed to parse Rollout"

    issues = []

    # progressDeadlineSeconds
    pds = rollout.get("spec", {}).get("progressDeadlineSeconds")
    if pds is None or not isinstance(pds, int) or pds > 1800:
        issues.append(f"progressDeadlineSeconds missing or >1800 (got {pds})")

    # progressDeadlineAbort
    pda = rollout.get("spec", {}).get("progressDeadlineAbort", False)
    if not pda:
        issues.append("progressDeadlineAbort not enabled")

    # Indefinite pause
    steps = rollout.get("spec", {}).get("strategy", {}).get("canary", {}).get("steps", [])
    for i, step in enumerate(steps):
        if "pause" in step:
            pause = step["pause"]
            if not pause or pause.get("duration") is None:
                issues.append(f"Step {i} has indefinite pause")

    # Pod stability — check for restarts on the CURRENT stable ReplicaSet only
    stable_rs = rollout.get("status", {}).get("stableRS", "")
    pod_selector = f"app=like-service"
    if stable_rs:
        pod_selector = f"app=like-service,rollouts-pod-template-hash={stable_rs}"
    rc, pods_json, _ = run_cmd(
        f"kubectl get pods -n bleater -l {pod_selector} -o json 2>/dev/null"
    )
    if pods_json:
        try:
            pods = json.loads(pods_json).get("items", [])
            for pod in pods:
                for cs in pod.get("status", {}).get("containerStatuses", []):
                    restarts = cs.get("restartCount", 0)
                    if restarts > 0:
                        issues.append(f"Pod {pod['metadata']['name']} has {restarts} restarts (liveness probe failing?)")
                    waiting = cs.get("state", {}).get("waiting", {})
                    if waiting.get("reason") in ("CrashLoopBackOff", "CreateContainerConfigError", "ImagePullBackOff"):
                        issues.append(f"Pod {pod['metadata']['name']}: {waiting['reason']}")
                for ics in pod.get("status", {}).get("initContainerStatuses", []):
                    waiting = ics.get("state", {}).get("waiting", {})
                    if waiting.get("reason") in ("CrashLoopBackOff", "Error"):
                        issues.append(f"Pod initContainer: {waiting['reason']}")
                    running = ics.get("state", {}).get("running", {})
                    if running and not ics.get("ready", False):
                        issues.append(f"Pod initContainer stuck running")
        except json.JSONDecodeError:
            pass

    # Missing ConfigMap
    env_from = rollout.get("spec", {}).get("template", {}).get("spec", {}).get(
        "containers", [{}])[0].get("envFrom", [])
    for ef in env_from:
        cm_ref = ef.get("configMapRef", {})
        if cm_ref and not cm_ref.get("optional", True):
            cm_name = cm_ref.get("name", "")
            rc, _, _ = run_cmd(f"kubectl get configmap {cm_name} -n bleater 2>/dev/null")
            if rc != 0:
                issues.append(f"Missing ConfigMap '{cm_name}' (optional=false)")

    # Broken initContainer
    init_containers = rollout.get("spec", {}).get("template", {}).get("spec", {}).get("initContainers", [])
    for ic in init_containers:
        cmd = " ".join(ic.get("command", []))
        if "10.99.99.99" in cmd:
            issues.append(f"initContainer references unreachable 10.99.99.99")

    # Broken liveness probe (wrong path)
    liveness = rollout.get("spec", {}).get("template", {}).get("spec", {}).get(
        "containers", [{}])[0].get("livenessProbe", {})
    if liveness:
        http_get = liveness.get("httpGet", {})
        if http_get.get("path") == "/healthz":
            issues.append("livenessProbe uses /healthz (app serves /health)")

    if not issues:
        return 1.0, f"Pods stable: deadline={pds}s, abort={pda}, 0 restarts, no issues"
    else:
        return 0.0, f"Issues: {'; '.join(issues)}"


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
        "enforcer_stopped_and_template_fixed": check_enforcer_stopped_and_template_fixed,
        "rollout_healthy_no_stale_ars": check_rollout_healthy_no_stale_ars,
        "canary_service_correctly_configured": check_canary_service_correctly_configured,
        "pods_stable_no_restarts": check_pods_stable_no_restarts,
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
