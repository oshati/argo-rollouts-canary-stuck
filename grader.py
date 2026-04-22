#!/usr/bin/env python3
"""
Grader for argo-rollouts-canary-stuck task.

4 FUNCTIONAL subscores, equal weight 0.25 each. Designed to be HARD:
each check verifies real behavior, not config existence.

1. analysis_query_semantic_correct
   - rate window >= 300s (3x the 60s scrape interval)
   - NaN-safe: query has `or on() vector(0)` / `or vector(0)` / similar
   - inconclusiveLimit >= 2 AND failureLimit >= 1
   - count >= 3 AND interval >= 60s (enough data points to matter)
   - SURVIVES the 180s durability window — multiple saboteurs try to revert

2. rollout_timeout_functional
   - progressDeadlineSeconds in [1, 1800]
   - progressDeadlineAbort: true
   - abortScaleDownDelaySeconds set
   - No indefinite pauses (every pause has a duration)
   - Rollout's Progressing condition is populated with a reason

3. canary_traffic_verified_flowing
   - Canary service's targetPort is not the sentinel (8099)
   - Endpoints populated and addresses belong to current canary RS pods
   - HTTP traffic from inside the cluster returns 2xx during the durability window
     (induced by the grader via kubectl exec against an in-cluster curl)

4. rollout_healthy_full_promotion
   - Rollout phase = Healthy
   - Last setWeight step reached (promotion complete)
   - At least one Successful AnalysisRun whose query is semantically correct
     (references bleater-like-service, has rate window >= 180s, is not a constant)
   - No Failed/Error/Inconclusive ARs remain
   - All rollout pods Running AND Ready for >= 30s
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


# ─────────────────────────────────────────────
# Shell helpers
# ─────────────────────────────────────────────
def run_cmd(cmd, timeout=30):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    except Exception as e:
        return 1, "", str(e)


def kget_json(resource, ns=None, name=None):
    ns_arg = f"-n {ns}" if ns else ""
    name_arg = name or ""
    rc, out, _ = run_cmd(f"kubectl get {resource} {name_arg} {ns_arg} -o json 2>/dev/null")
    if rc != 0 or not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


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
# TRAFFIC INDUCTION (O8)
# During the 180s durability window, spawn an in-cluster pod that curls the
# canary service in a loop and records total/2xx-3xx-4xx counts to a file.
# We use a pod (not kubectl exec) so we don't depend on app images having curl.
# The pod runs with label role=grader-traffic, which is NOT in the
# NetworkPolicy allow-list — so traffic only flows if the agent removed/fixed
# the NetworkPolicy (and fixed canary targetPort + pod readiness).
# ─────────────────────────────────────────────
TRAFFIC_STATE = {"total": 0, "success": 0}
TRAFFIC_POD = "grader-traffic-gen"
TRAFFIC_IMAGE = "docker.io/bitnamilegacy/postgresql:17.0.0-debian-12-r11"


def traffic_start(setup_info):
    like_port = setup_info.get("LIKE_PORT", "8006")
    # Clean up any leftover pod
    run_cmd(
        f"kubectl delete pod {TRAFFIC_POD} -n bleater --force --grace-period=0 "
        "--ignore-not-found=true 2>/dev/null",
        timeout=20,
    )
    time.sleep(2)
    # Spawn a traffic pod. Labels: role=grader-traffic (deliberately NOT the
    # label the NetworkPolicy allows — agent must neutralize the NP).
    spec = f"""
apiVersion: v1
kind: Pod
metadata:
  name: {TRAFFIC_POD}
  namespace: bleater
  labels:
    role: grader-traffic
    app: grader-traffic-gen
spec:
  restartPolicy: Never
  containers:
  - name: gen
    image: {TRAFFIC_IMAGE}
    imagePullPolicy: IfNotPresent
    command: ["/bin/bash", "-c"]
    args:
    - |
      total=0; ok=0
      URL=http://bleater-like-service-canary.bleater.svc.cluster.local:{like_port}/
      probe() {{
        if command -v curl >/dev/null 2>&1; then
          curl -s -o /dev/null -w '%{{http_code}}' --max-time 3 "$URL" 2>/dev/null
        else
          python3 -c "import urllib.request as u, sys
try:
  r = u.urlopen('$URL', timeout=3)
  print(r.status)
except Exception:
  print(0)
" 2>/dev/null || echo 000
        fi
      }}
      while true; do
        code=$(probe)
        total=$((total+1))
        if [ "$code" -ge 200 ] 2>/dev/null && [ "$code" -lt 500 ] 2>/dev/null; then
          ok=$((ok+1))
        fi
        echo "$total $ok" > /tmp/counters
        sleep 2
      done
    resources:
      requests: {{cpu: "10m", memory: "32Mi"}}
      limits: {{cpu: "100m", memory: "64Mi"}}
"""
    # Apply via stdin
    proc = subprocess.run(
        "kubectl apply -f - 2>&1",
        input=spec, shell=True, capture_output=True, text=True, timeout=30,
    )
    # Wait up to 30s for the pod to be Running
    for _ in range(30):
        rc, phase, _ = run_cmd(
            f"kubectl get pod {TRAFFIC_POD} -n bleater -o jsonpath='{{.status.phase}}' 2>/dev/null"
        )
        if phase == "Running":
            return
        time.sleep(1)


def traffic_stop():
    # Read the final counter snapshot from the pod
    rc, out, _ = run_cmd(
        f"kubectl exec -n bleater {TRAFFIC_POD} -- cat /tmp/counters 2>/dev/null",
        timeout=15,
    )
    if out:
        parts = out.strip().split()
        if len(parts) == 2:
            try:
                TRAFFIC_STATE["total"] = int(parts[0])
                TRAFFIC_STATE["success"] = int(parts[1])
            except ValueError:
                pass
    # Clean up
    run_cmd(
        f"kubectl delete pod {TRAFFIC_POD} -n bleater --force --grace-period=0 "
        "--ignore-not-found=true --wait=false 2>/dev/null",
        timeout=15,
    )


# ─────────────────────────────────────────────
# CHECK 1: analysis_query_semantic_correct
# ─────────────────────────────────────────────
def check_analysis_query_semantic_correct(setup_info):
    """
    Semantic audit of the AnalysisTemplate AFTER the durability window.
    All of these must be true simultaneously:
      - rate window >= 300s
      - NaN-safe (query contains `or on() vector(...)` or `or vector(...)`
        OR metric has `nanStrategy` annotation)
      - inconclusiveLimit >= 2 and failureLimit >= 1
      - count >= 3 and interval >= 60s
      - query not a trivial constant (must reference http_requests_total AND
        the service label)
    """
    t = kget_json("analysistemplate", ns="bleater", name="bleater-like-service-error-rate")
    if not t:
        return 0.0, "AnalysisTemplate missing — saboteurs likely reverted it"

    metrics = t.get("spec", {}).get("metrics", [])
    if not metrics:
        return 0.0, "No metrics in AnalysisTemplate"
    m = metrics[0]
    query = (m.get("provider", {}).get("prometheus", {}).get("query") or "").strip()
    if not query:
        return 0.0, "Empty Prometheus query"

    issues = []

    # Rate window >= 300s
    rate_matches = re.findall(r"rate\([^[]*\[(\d+)([smh])\]", query)
    if not rate_matches:
        issues.append("no rate() window found")
        window_s = 0
    else:
        value, unit = rate_matches[0]
        window_s = int(value) * {"s": 1, "m": 60, "h": 3600}.get(unit, 1)
        if window_s < 300:
            issues.append(f"rate window {window_s}s < 300s required")

    # Must reference the service by label (not a trivial constant)
    if "http_requests_total" not in query:
        issues.append("query does not reference http_requests_total metric")
    if "bleater-like-service" not in query:
        issues.append("query does not reference bleater-like-service by label")
    # Reject blatantly trivial constants
    if re.search(r"^\s*vector\s*\(\s*[01]\s*\)\s*$", query) or \
       re.search(r"^\s*scalar\s*\(\s*vector\s*\(", query):
        issues.append("query is a trivial constant")

    # NaN safety: `or on() vector(...)` OR `or vector(...)` OR nanStrategy field
    nan_safe = bool(re.search(r"\bor\s+(on\s*\(\s*\)\s+)?vector\s*\(", query))
    # Some providers expose nanStrategy — accept either mechanism
    if not nan_safe:
        prov = m.get("provider", {}).get("prometheus", {})
        if not prov.get("nanStrategy"):
            issues.append("query not NaN-safe: add `or on() vector(0)` or similar")

    # inconclusiveLimit and failureLimit
    inc = m.get("inconclusiveLimit")
    if not isinstance(inc, int) or inc < 2:
        issues.append(f"inconclusiveLimit={inc} (need >= 2)")
    fl = m.get("failureLimit")
    if not isinstance(fl, int) or fl < 1:
        issues.append(f"failureLimit={fl} (need >= 1)")

    # count >= 3
    count = m.get("count")
    if not isinstance(count, int) or count < 3:
        issues.append(f"count={count} (need >= 3)")

    # interval >= 60s
    interval = (m.get("interval") or "").strip()
    sec = 0
    mtch = re.match(r"(\d+)([smh])", interval) if interval else None
    if mtch:
        sec = int(mtch.group(1)) * {"s": 1, "m": 60, "h": 3600}[mtch.group(2)]
    if sec < 60:
        issues.append(f"interval={interval} (need >= 60s)")

    if issues:
        return 0.0, "; ".join(issues)
    return 1.0, (
        f"Semantic OK: window={window_s}s, count={count}, interval={interval}, "
        f"inconclusiveLimit={inc}, failureLimit={fl}"
    )


# ─────────────────────────────────────────────
# CHECK 2: rollout_timeout_functional
# ─────────────────────────────────────────────
def check_rollout_timeout_functional(setup_info):
    """
    Structural + condition check for timeout protection:
      - progressDeadlineSeconds in [1, 1800]
      - progressDeadlineAbort: true
      - abortScaleDownDelaySeconds set (int, >=0)
      - No indefinite pauses
      - Analysis count>=3, interval>=60s (referencing check 1 semantics)
      - Rollout conditions contain Progressing entry with non-empty reason
    """
    r = kget_json("rollout", ns="bleater", name="bleater-like-service")
    if not r:
        return 0.0, "Rollout not found"

    issues = []
    spec = r.get("spec", {})

    pds = spec.get("progressDeadlineSeconds")
    if not isinstance(pds, int) or not (1 <= pds <= 1800):
        issues.append(f"progressDeadlineSeconds={pds} (need 1..1800)")

    if spec.get("progressDeadlineAbort") is not True:
        issues.append("progressDeadlineAbort must be true")

    asd = spec.get("strategy", {}).get("canary", {}).get("abortScaleDownDelaySeconds")
    if not isinstance(asd, int):
        issues.append("canary.abortScaleDownDelaySeconds must be set (int)")

    steps = spec.get("strategy", {}).get("canary", {}).get("steps", []) or []
    for i, step in enumerate(steps):
        if "pause" in step:
            pause = step.get("pause") or {}
            if pause.get("duration") in (None, "", 0):
                issues.append(f"step {i} has indefinite pause (no duration)")

    # Rollout conditions must exist (controller actually saw it)
    conds = r.get("status", {}).get("conditions", []) or []
    prog = next((c for c in conds if c.get("type") == "Progressing"), None)
    if not prog or not prog.get("reason"):
        issues.append("Progressing condition missing or has no reason")

    # Sanity on analysis step bounds — enforce via the inline analysis ref
    analysis_ref = spec.get("strategy", {}).get("canary", {}).get("analysis", {})
    if not analysis_ref.get("templates"):
        issues.append("canary.analysis.templates not set on rollout")

    if issues:
        return 0.0, "; ".join(issues)
    return 1.0, (
        f"Timeout functional: pds={pds}, abort=true, "
        f"abortScaleDownDelaySeconds={asd}, all pauses timed, "
        f"Progressing.reason={prog.get('reason')}"
    )


# ─────────────────────────────────────────────
# CHECK 3: canary_traffic_verified_flowing
# ─────────────────────────────────────────────
def check_canary_traffic_verified_flowing(setup_info):
    """
    Functional traffic test:
      - Canary service exists with targetPort != 8099
      - Endpoints populated, addresses belong to canary RS pods
      - Traffic induced during durability window had >=70% 2xx rate
        with >=10 attempts total
    """
    r = kget_json("rollout", ns="bleater", name="bleater-like-service")
    if not r:
        return 0.0, "Rollout not found"
    canary_svc = r.get("spec", {}).get("strategy", {}).get("canary", {}).get("canaryService", "")
    if not canary_svc:
        return 0.0, "canaryService not declared in Rollout"

    svc = kget_json("service", ns="bleater", name=canary_svc)
    if not svc:
        return 0.0, f"Canary service {canary_svc} not found"

    ports = svc.get("spec", {}).get("ports", []) or []
    if not ports:
        return 0.0, "Canary service has no ports"
    target_port = ports[0].get("targetPort")
    if target_port == 8099:
        return 0.0, f"Canary targetPort still 8099 (sentinel broken value)"

    # Endpoints must be populated
    rc, eps_json, _ = run_cmd(
        f"kubectl get endpoints {canary_svc} -n bleater -o json 2>/dev/null"
    )
    if rc != 0 or not eps_json:
        return 0.0, "Canary endpoints not found"
    try:
        eps = json.loads(eps_json)
    except json.JSONDecodeError:
        return 0.0, "Failed to parse endpoints"
    subsets = eps.get("subsets") or []
    addresses = []
    for s in subsets:
        for a in (s.get("addresses") or []):
            addresses.append(a.get("ip"))
    if not addresses:
        return 0.0, "Canary endpoints have no addresses"

    # Traffic counts collected during durability window
    total = TRAFFIC_STATE["total"]
    ok = TRAFFIC_STATE["success"]
    if total < 10:
        return 0.0, (
            f"Traffic sampling insufficient (total={total}); "
            "agent likely blocked induced traffic (NetworkPolicy?)"
        )
    rate = ok / total
    if rate < 0.7:
        return 0.0, (
            f"Canary 2xx rate {ok}/{total}={rate:.0%} < 70% during durability window"
        )

    return 1.0, (
        f"Traffic OK: targetPort={target_port}, {len(addresses)} endpoints, "
        f"2xx {ok}/{total}={rate:.0%}"
    )


# ─────────────────────────────────────────────
# CHECK 4: rollout_healthy_full_promotion
# ─────────────────────────────────────────────
def check_rollout_healthy_full_promotion(setup_info):
    """
    End-to-end: rollout is fully promoted AND cleanly succeeded.
      - phase == Healthy
      - last setWeight step reached (final weight in the steps list)
      - >=1 Successful AR with a semantically-correct query
      - No Failed/Error/Inconclusive ARs
      - Rollout pods Running AND Ready for >=30s
    """
    r = kget_json("rollout", ns="bleater", name="bleater-like-service")
    if not r:
        return 0.0, "Rollout not found"

    phase = (r.get("status", {}).get("phase") or "").lower()
    if phase != "healthy":
        return 0.0, f"Rollout phase={phase} (need Healthy)"

    # Verify the controller actually finished all setWeight steps
    steps = r.get("spec", {}).get("strategy", {}).get("canary", {}).get("steps", []) or []
    weights = [s.get("setWeight") for s in steps if s.get("setWeight") is not None]
    final_weight = weights[-1] if weights else 100
    current_step_idx = r.get("status", {}).get("currentStepIndex", -1)
    if current_step_idx < len(steps):
        return 0.0, f"Not promoted: currentStepIndex={current_step_idx}/{len(steps)}"

    # ARs: require at least one Successful AND zero bad
    ars = kget_json("analysisrun", ns="bleater") or {}
    has_good_successful = False
    bad = []
    for ar in ars.get("items", []) or []:
        p = (ar.get("status", {}).get("phase") or "").lower()
        if p in ("failed", "error", "inconclusive"):
            bad.append(ar.get("metadata", {}).get("name"))
        if p == "successful":
            # Semantic check on this AR's query
            metrics = ar.get("spec", {}).get("metrics", []) or []
            if metrics:
                q = (metrics[0].get("provider", {}).get("prometheus", {}).get("query") or "")
                rate_matches = re.findall(r"rate\([^[]*\[(\d+)([smh])\]", q)
                if rate_matches:
                    v, u = rate_matches[0]
                    w = int(v) * {"s": 1, "m": 60, "h": 3600}.get(u, 1)
                    if (
                        w >= 180
                        and "http_requests_total" in q
                        and "bleater-like-service" in q
                    ):
                        has_good_successful = True
    if bad:
        return 0.0, f"Stale bad ARs remain: {bad}"
    if not has_good_successful:
        return 0.0, "No Successful AR with a semantically-correct query"

    # Pods Running AND Ready for >= 30s
    rc, pods_json, _ = run_cmd(
        "kubectl get pod -n bleater -l app=like-service -o json 2>/dev/null"
    )
    try:
        pods = json.loads(pods_json).get("items", []) if pods_json else []
    except json.JSONDecodeError:
        pods = []
    if not pods:
        return 0.0, "No like-service pods found"
    now = time.time()
    stable = 0
    for p in pods:
        phase_p = p.get("status", {}).get("phase")
        if phase_p != "Running":
            continue
        ready = False
        ready_transition = None
        for c in p.get("status", {}).get("conditions", []) or []:
            if c.get("type") == "Ready":
                ready = c.get("status") == "True"
                lt = c.get("lastTransitionTime")
                if lt:
                    try:
                        ready_transition = time.mktime(
                            time.strptime(lt, "%Y-%m-%dT%H:%M:%SZ")
                        )
                    except ValueError:
                        ready_transition = None
        if ready and ready_transition and (now - ready_transition) >= 30:
            stable += 1
    if stable == 0:
        return 0.0, "No pods have been Ready for >=30s"

    return 1.0, (
        f"Healthy + promoted to {final_weight}%, {stable} pods stable-ready, "
        f"Successful AR with valid query, no bad ARs"
    )


# ─────────────────────────────────────────────
# GRADE
# ─────────────────────────────────────────────
def grade(*args, **kwargs) -> GradingResult:
    os.environ["KUBECONFIG"] = "/etc/rancher/k3s/k3s.yaml"
    setup_info = load_setup_info()

    # Kill any lingering agent processes so they can't mutate state during grading.
    run_cmd("pkill -u ubuntu 2>/dev/null || true")

    # Launch in-cluster traffic during the durability window.
    print("[grader] Inducing canary traffic for 180s durability window...")
    traffic_start(setup_info)
    time.sleep(180)
    traffic_stop()
    print(
        f"[grader] Traffic sampled: {TRAFFIC_STATE['success']}/"
        f"{TRAFFIC_STATE['total']} 2xx"
    )

    checks = {
        "analysis_query_semantic_correct": check_analysis_query_semantic_correct,
        "rollout_timeout_functional": check_rollout_timeout_functional,
        "canary_traffic_verified_flowing": check_canary_traffic_verified_flowing,
        "rollout_healthy_full_promotion": check_rollout_healthy_full_promotion,
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

    total = sum(subscores[k] * weights[k] for k in subscores)
    print(f"\n[grader] Final score: {total:.4f}")
    return GradingResult(
        score=total,
        subscores=subscores,
        weights=weights,
        feedback="\n".join(feedback_parts),
    )


if __name__ == "__main__":
    result = grade()
    print(f"\nScore: {result.score}")
    print(f"Subscores: {result.subscores}")
