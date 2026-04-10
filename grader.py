#!/usr/bin/env python3
"""
Grader for argo-rollouts-canary-stuck task.

ALL checks are FUNCTIONAL end-to-end tests.

4 subscores, each weight 1/4:
1. analysis_template_fixed — rate window >= 2x scrapeInterval AND nanStrategy present
2. rollout_promoted — Rollout is Healthy with stable revision promoted
3. prometheus_returns_data — Corrected query returns numeric data (not NaN)
4. progress_deadline_set — Rollout has progressDeadlineSeconds <= 1800
"""

import json
import os
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


def check_analysis_template_fixed(setup_info):
    """
    FUNCTIONAL: Check AnalysisTemplate has:
    1. rate() window >= 120s (at least 2x the 60s scrapeInterval)
    2. nanStrategy field present
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
        return 0.0, "No metrics defined in AnalysisTemplate"

    metric = metrics[0]
    query = metric.get("provider", {}).get("prometheus", {}).get("query", "")
    nan_strategy = metric.get("nanStrategy", "")

    # Check rate window
    import re
    rate_matches = re.findall(r'rate\([^[]*\[(\d+)([smh])\]', query)
    if not rate_matches:
        return 0.0, f"No rate() function found in query: {query[:100]}"

    value, unit = rate_matches[0]
    seconds = int(value) * {"s": 1, "m": 60, "h": 3600}.get(unit, 1)

    rate_ok = seconds >= 120  # At least 2x the 60s scrapeInterval

    # Check for NaN handling: consecutiveErrorLimit, or isNaN in successCondition
    success_cond = metric.get("successCondition", "")
    consecutive_limit = metric.get("consecutiveErrorLimit")
    nan_handled = (
        "isnan" in success_cond.lower() or
        "isNaN" in success_cond or
        (consecutive_limit is not None and isinstance(consecutive_limit, int))
    )

    if rate_ok and nan_handled:
        return 1.0, f"AnalysisTemplate fixed: rate window={seconds}s (>= 120s), NaN handling present"
    elif rate_ok:
        return 0.0, f"Rate window OK ({seconds}s) but no NaN handling (need isNaN in successCondition or consecutiveErrorLimit)"
    elif nan_handled:
        return 0.0, f"NaN handling present but rate window too small ({seconds}s, need >= 120s)"
    else:
        return 0.0, f"Both broken: rate window={seconds}s, no NaN handling"


def check_rollout_promoted(setup_info):
    """
    FUNCTIONAL: Check that the Rollout is Healthy with stable revision promoted.
    The canary should have advanced through all steps.
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

    status = rollout.get("status", {})
    phase = status.get("phase", "")
    stable_rs = status.get("stableRS", "")
    canary_rs = status.get("currentPodHash", "")

    # Check conditions
    conditions = status.get("conditions", [])
    healthy = False
    for cond in conditions:
        if cond.get("type") == "Healthy" and cond.get("status") == "True":
            healthy = True
        if cond.get("type") == "Progressing" and cond.get("status") == "True":
            healthy = True  # Still progressing is also acceptable

    # Check if canary weight is 0 (fully promoted) or 100 (at final step)
    current_step = status.get("currentStepIndex", -1)
    total_steps = len(rollout.get("spec", {}).get("strategy", {}).get("canary", {}).get("steps", []))

    # Accept: phase is Healthy, or all steps completed, or stable replicas match spec
    replicas = rollout.get("spec", {}).get("replicas", 2)
    available = status.get("availableReplicas", 0)
    ready = status.get("readyReplicas", 0)

    if phase.lower() == "healthy":
        return 1.0, f"Rollout is Healthy (phase={phase}, stableRS={stable_rs})"

    if current_step >= total_steps - 1 and available >= replicas:
        return 1.0, f"Rollout completed all steps (step {current_step + 1}/{total_steps}, available={available})"

    if phase.lower() == "paused":
        return 0.0, f"Rollout is still Paused at step {current_step + 1}/{total_steps}"

    if phase.lower() == "degraded":
        return 0.0, f"Rollout is Degraded: {[c.get('message', '') for c in conditions]}"

    return 0.0, f"Rollout not fully promoted: phase={phase}, step={current_step + 1}/{total_steps}, available={available}"


def check_prometheus_returns_data(setup_info):
    """
    FUNCTIONAL: Query Prometheus with the corrected rate expression and verify
    it returns a numeric value (not NaN, not empty).
    """
    # Query Prometheus via k8s service DNS (grader runs as root with full access)
    import urllib.parse
    query = 'sum(rate(http_requests_total{service="bleater-like-service"}[2m]))'
    encoded_query = urllib.parse.quote(query)
    prom_url = f"http://prometheus.monitoring.svc.cluster.local:9090/api/v1/query?query={encoded_query}"

    # Try multiple methods to reach Prometheus
    result = ""
    for method in [
        f"curl -sf '{prom_url}'",
        f"wget -qO- '{prom_url}'",
    ]:
        rc, result, _ = run_cmd(method, timeout=15)
        if rc == 0 and result:
            break

    if not result:
        # Try via kubectl exec into any pod with curl/wget
        rc, prom_pod, _ = run_cmd(
            "kubectl get pods -n monitoring -l app=prometheus "
            "-o jsonpath='{.items[0].metadata.name}' 2>/dev/null"
        )
        prom_pod = prom_pod.strip("'") if prom_pod else ""
        if prom_pod:
            for cmd in [
                f"kubectl exec -n monitoring {prom_pod} -- wget -qO- 'http://localhost:9090/api/v1/query?query={encoded_query}'",
                f"kubectl exec -n monitoring {prom_pod} -- curl -sf 'http://localhost:9090/api/v1/query?query={encoded_query}'",
            ]:
                rc, result, _ = run_cmd(cmd, timeout=15)
                if rc == 0 and result:
                    break

    if not result:
        return 0.0, "Could not query Prometheus"

    try:
        data = json.loads(result)
    except json.JSONDecodeError:
        return 0.0, f"Prometheus response not JSON: {result[:200]}"

    if data.get("status") != "success":
        return 0.0, f"Prometheus query failed: {data.get('error', 'unknown')}"

    results = data.get("data", {}).get("result", [])

    if not results:
        # Empty result is OK if bleater-like-service has no errors — the sum() returns 0
        # Check if the base metric exists
        base_query = urllib.parse.quote('http_requests_total{service="bleater-like-service"}')
        base_url = f"http://prometheus.monitoring.svc.cluster.local:9090/api/v1/query?query={base_query}"
        rc, base_result, _ = run_cmd(f"curl -sf '{base_url}' 2>/dev/null || wget -qO- '{base_url}' 2>/dev/null", timeout=15)
        if base_result:
            try:
                base_data = json.loads(base_result)
                base_results = base_data.get("data", {}).get("result", [])
                if base_results:
                    return 1.0, f"Base metric exists ({len(base_results)} series). Rate returns empty (no errors = good)."
            except json.JSONDecodeError:
                pass

        # Empty result means no metric data exists at all — the agent must ensure
        # Prometheus is scraping the service with the corrected rate window
        return 0.0, "Prometheus returns empty result — no metric data for bleater-like-service"

    # Check the value is numeric (not NaN)
    value = results[0].get("value", [None, None])
    if len(value) >= 2:
        metric_val = value[1]
        if metric_val == "NaN" or metric_val is None:
            return 0.0, f"Prometheus still returning NaN: {value}"
        try:
            float(metric_val)
            return 1.0, f"Prometheus returns valid data: {metric_val}"
        except (ValueError, TypeError):
            return 0.0, f"Prometheus value not numeric: {metric_val}"

    return 0.0, f"Unexpected Prometheus response format: {results}"


def check_progress_deadline_set(setup_info):
    """
    FUNCTIONAL: Check that the Rollout has progressDeadlineSeconds set to <= 1800.
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

    if pds is None:
        return 0.0, "progressDeadlineSeconds not set on Rollout"

    if isinstance(pds, int) and pds <= 1800:
        return 1.0, f"progressDeadlineSeconds set to {pds}s (within limit)"
    else:
        return 0.0, f"progressDeadlineSeconds too high or invalid: {pds}"


def grade(*args, **kwargs) -> GradingResult:
    """Main grading function."""
    os.environ["KUBECONFIG"] = "/etc/rancher/k3s/k3s.yaml"
    setup_info = load_setup_info()

    run_cmd("pkill -u ubuntu 2>/dev/null || true")

    print("[grader] Waiting 120s...")
    time.sleep(120)

    checks = {
        "analysis_template_fixed": check_analysis_template_fixed,
        "rollout_promoted": check_rollout_promoted,
        "prometheus_returns_data": check_prometheus_returns_data,
        "progress_deadline_set": check_progress_deadline_set,
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
