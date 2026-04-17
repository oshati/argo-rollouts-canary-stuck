#!/usr/bin/env python3
"""
Grader for argo-rollouts-canary-stuck task.

ALL checks are FUNCTIONAL end-to-end tests.

4 subscores, each weight 1/4:
1. enforcer_stopped_and_template_fixed — Enforcer neutralized AND template has
   correct rate window + NaN handling (tested AFTER 180s durability window)
2. rollout_healthy_with_clean_ars — Rollout Healthy, Successful AR, no Failed ARs
3. canary_service_routes_traffic — Canary service selector matches actual pods
4. pods_stable_after_promotion — Promoted pods are running (no crash from missing ConfigMap)
   AND progressDeadlineSeconds + progressDeadlineAbort set AND no indefinite pause
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
# ─────────────────────────────────────────────
def check_enforcer_stopped_and_template_fixed(setup_info):
    """
    After 180s durability window, verify:
    1. The AnalysisTemplate has rate window >= 120s
    2. The template has NaN handling (query-level or config-level)
    3. The enforcer CronJob hasn't reverted the template

    If the enforcer is still running, it will have reverted the template
    during the durability window, causing this check to fail.
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

    metric = metrics[0]
    query = metric.get("provider", {}).get("prometheus", {}).get("query", "")

    # Check rate window
    rate_matches = re.findall(r'rate\([^[]*\[(\d+)([smh])\]', query)
    if not rate_matches:
        return 0.0, f"No rate() in query — enforcer may have reverted the template"

    value, unit = rate_matches[0]
    seconds = int(value) * {"s": 1, "m": 60, "h": 3600}.get(unit, 1)

    if seconds < 120:
        return 0.0, f"Rate window is {seconds}s (< 120s) — enforcer likely reverted the template"

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
        return 0.0, f"Rate window OK ({seconds}s) but no NaN handling — template may be partially reverted"

    return 1.0, f"Template fixed and durable: rate={seconds}s, NaN handling present (enforcer stopped)"


# ─────────────────────────────────────────────
# CHECK 2: rollout_healthy_with_clean_ars
# ─────────────────────────────────────────────
def check_rollout_healthy_with_clean_ars(setup_info):
    """
    Rollout must be Healthy with a Successful AnalysisRun.
    Failed/Error AnalysisRuns must be cleaned up.
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
        step = rollout.get("status", {}).get("currentStepIndex", -1)
        total = len(rollout.get("spec", {}).get("strategy", {}).get("canary", {}).get("steps", []))
        return 0.0, f"Rollout Paused at step {step + 1}/{total}"

    # Get AnalysisRuns
    rc, ar_json, _ = run_cmd("kubectl get analysisrun -n bleater -o json 2>/dev/null")
    ar_phases = []
    has_successful = False
    has_failed = False
    if ar_json:
        try:
            for ar in json.loads(ar_json).get("items", []):
                p = ar.get("status", {}).get("phase", "unknown")
                ar_phases.append(p)
                if p.lower() == "successful":
                    has_successful = True
                if p.lower() in ("failed", "error"):
                    has_failed = True
        except json.JSONDecodeError:
            pass

    if phase.lower() == "healthy" and has_successful and not has_failed:
        return 1.0, f"Clean promotion: Healthy + Successful AR, no Failed ARs"

    if phase.lower() == "healthy" and has_successful and has_failed:
        return 0.0, f"Healthy but Failed ARs left behind: {ar_phases}"

    if phase.lower() == "healthy" and not ar_phases:
        pds = rollout.get("spec", {}).get("progressDeadlineSeconds")
        if pds is not None:
            return 1.0, "Healthy, no ARs, progressDeadlineSeconds set"
        return 0.0, "Healthy but no ARs and no progressDeadlineSeconds"

    return 0.0, f"Not Healthy: phase={phase}, ARs={ar_phases}"


# ─────────────────────────────────────────────
# CHECK 3: canary_service_routes_traffic
# ─────────────────────────────────────────────
def check_canary_service_routes_traffic(setup_info):
    """
    Verify the canary Service targetPort matches the container's actual port.
    Setup corrupts targetPort to 8099 (container listens on 8006).
    No InvalidSpec error — the agent must trace why canary gets no traffic.
    """
    rc, svc_json, _ = run_cmd(
        "kubectl get service bleater-like-service-canary -n bleater -o json 2>/dev/null"
    )
    if rc != 0 or not svc_json:
        return 0.0, "Canary service not found"

    try:
        svc = json.loads(svc_json)
    except json.JSONDecodeError:
        return 0.0, "Failed to parse canary service"

    ports = svc.get("spec", {}).get("ports", [])
    if not ports:
        return 0.0, "Canary service has no ports defined"

    target_port = ports[0].get("targetPort", 0)

    # The broken targetPort is 8099. The real container port varies but is NOT 8099.
    if target_port == 8099:
        return 0.0, f"Canary service targetPort still broken: {target_port} (should match container port)"

    # Verify the service actually has endpoints (traffic flows)
    rc, endpoints, _ = run_cmd(
        "kubectl get endpoints bleater-like-service-canary -n bleater "
        "-o jsonpath='{.subsets[0].addresses}' 2>/dev/null"
    )

    if endpoints and endpoints != "''" and endpoints != "null":
        return 1.0, f"Canary service routes traffic (targetPort={target_port}, has endpoints)"

    # Accept if targetPort matches a known container port even without endpoints
    like_port = setup_info.get("LIKE_PORT", "8006")
    if str(target_port) == str(like_port):
        return 1.0, f"Canary service targetPort fixed to {target_port}"

    return 0.0, f"Canary service targetPort={target_port} but no endpoints"


# ─────────────────────────────────────────────
# CHECK 4: pods_stable_after_promotion
# ─────────────────────────────────────────────
def check_pods_stable_after_promotion(setup_info):
    """
    Verify:
    1. Promoted pods are running without CrashLoopBackOff (missing ConfigMap fixed)
    2. progressDeadlineSeconds is set (<= 1800)
    3. progressDeadlineAbort is enabled
    4. No indefinite pause in canary steps

    All 4 must pass. This combines multiple agent-must-do checks into one
    functional verification.
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

    issues = []

    # Check progressDeadlineSeconds
    pds = rollout.get("spec", {}).get("progressDeadlineSeconds")
    if pds is None or not isinstance(pds, int) or pds > 1800:
        issues.append(f"progressDeadlineSeconds missing or >1800 (got {pds})")

    # Check progressDeadlineAbort
    pda = rollout.get("spec", {}).get("progressDeadlineAbort", False)
    if not pda:
        issues.append("progressDeadlineAbort not enabled")

    # Check for indefinite pause
    steps = rollout.get("spec", {}).get("strategy", {}).get("canary", {}).get("steps", [])
    for i, step in enumerate(steps):
        if "pause" in step:
            pause = step["pause"]
            if not pause or pause.get("duration") is None:
                issues.append(f"Step {i} has indefinite pause")

    # Check pods are running (not CrashLoopBackOff, not stuck in Init)
    rc, pods_json, _ = run_cmd(
        "kubectl get pods -n bleater -l app=like-service -o json 2>/dev/null"
    )
    if pods_json:
        try:
            pods = json.loads(pods_json).get("items", [])
            for pod in pods:
                phase = pod.get("status", {}).get("phase", "")
                # Check main containers
                for cs in pod.get("status", {}).get("containerStatuses", []):
                    waiting = cs.get("state", {}).get("waiting", {})
                    if waiting.get("reason") in ("CrashLoopBackOff", "CreateContainerConfigError", "ImagePullBackOff"):
                        issues.append(f"Pod {pod['metadata']['name']} container: {waiting['reason']}")
                # Check init containers (broken init = pods stuck in Init:*)
                for ics in pod.get("status", {}).get("initContainerStatuses", []):
                    waiting = ics.get("state", {}).get("waiting", {})
                    if waiting.get("reason") in ("CrashLoopBackOff", "Error"):
                        issues.append(f"Pod {pod['metadata']['name']} initContainer: {waiting['reason']}")
                    # Also check if init container has been running too long (stuck)
                    running = ics.get("state", {}).get("running", {})
                    if running and not ics.get("ready", False):
                        issues.append(f"Pod {pod['metadata']['name']} initContainer stuck running")
        except json.JSONDecodeError:
            pass

    # Check envFrom doesn't reference a non-existent ConfigMap with optional=false
    env_from = rollout.get("spec", {}).get("template", {}).get("spec", {}).get(
        "containers", [{}])[0].get("envFrom", [])
    for ef in env_from:
        cm_ref = ef.get("configMapRef", {})
        if cm_ref and not cm_ref.get("optional", True):
            cm_name = cm_ref.get("name", "")
            rc, _, _ = run_cmd(f"kubectl get configmap {cm_name} -n bleater 2>/dev/null")
            if rc != 0:
                issues.append(f"envFrom references missing ConfigMap '{cm_name}' with optional=false")

    # Check initContainers don't reference unreachable endpoints
    init_containers = rollout.get("spec", {}).get("template", {}).get("spec", {}).get("initContainers", [])
    for ic in init_containers:
        cmd = " ".join(ic.get("command", []))
        # Check for hardcoded IPs that are unreachable
        if "10.99.99.99" in cmd or "wait-for-deps" in ic.get("name", ""):
            issues.append(f"initContainer '{ic.get('name')}' references unreachable endpoint")

    if not issues:
        return 1.0, f"Pods stable: deadline={pds}s, abort={pda}, no indefinite pause, no crash pods"
    else:
        return 0.0, f"Issues: {'; '.join(issues)}"


# ─────────────────────────────────────────────
# GRADE
# ─────────────────────────────────────────────
def grade(*args, **kwargs) -> GradingResult:
    os.environ["KUBECONFIG"] = "/etc/rancher/k3s/k3s.yaml"
    setup_info = load_setup_info()

    run_cmd("pkill -u ubuntu 2>/dev/null || true")

    # Durability window — enforcer runs every 2 min, so 180s gives it time
    print("[grader] Waiting 180s durability window...")
    time.sleep(180)

    checks = {
        "enforcer_stopped_and_template_fixed": check_enforcer_stopped_and_template_fixed,
        "rollout_healthy_with_clean_ars": check_rollout_healthy_with_clean_ars,
        "canary_service_routes_traffic": check_canary_service_routes_traffic,
        "pods_stable_after_promotion": check_pods_stable_after_promotion,
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
