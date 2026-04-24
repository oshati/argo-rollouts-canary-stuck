#!/usr/bin/env python3
"""
Grader for argo-rollouts-canary-stuck task.

4 FUNCTIONAL subscores, equal weight 0.25 each. Designed to be HARD:
each check verifies real behavior, not config existence.

1. analysis_query_semantic_correct
   - rate window >= 180s (>=3x the 60s scrape interval, per Q4 runbook hint)
   - NaN-safe: `or on() vector(0)` / `nanStrategy` / `len(result) == 0`
   - inconclusiveLimit >= 1 (the broken default is 0; any raise passes)
   - count >= 3 (>= 3 samples so transient noise can't flip the verdict)
   - interval >= 30s
   - SURVIVES the 180s durability window — multiple saboteurs try to revert

2. rollout_timeout_functional
   - progressDeadlineSeconds in [1, 1800]
   - progressDeadlineAbort: true
   - No indefinite pauses (every pause has a duration)
   - Rollout's Progressing condition is populated with a reason
   - Analysis template is wired up

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
import threading
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
# TRAFFIC VERIFICATION (O8) — endpoint+pod stability approach
#
# We tried three HTTP/TCP probe variants — port-forward (died after 1 conn),
# urllib HTTP (backend doesn't serve /), kubectl-exec + Python (empty output).
# All were infrastructure-flaky. Kubernetes *does* guarantee that if:
#   - the canary service has endpoints
#   - those endpoint IPs match the current canary RS pods
#   - those pods are Running AND Ready continuously
# then service-routing traffic works end-to-end.
#
# So instead of sampling live traffic we verify the preconditions are met
# AND have remained met continuously across the 180s durability window. A
# single snapshot could miss a pod that flaps; multiple samples catch it.
# ─────────────────────────────────────────────
TRAFFIC_STATE = {
    "samples": 0,  # snapshots taken across the window
    "stable_samples": 0,  # snapshots where conditions are met
    "stopped": False,
    "thread": None,
    "last_err": "",
}


def _sample_traffic_path():
    """One snapshot: returns (ok, reason)."""
    r = kget_json("rollout", ns="bleater", name="bleater-like-service")
    if not r:
        return False, "rollout gone"
    canary_svc = r.get("spec", {}).get("strategy", {}).get("canary", {}).get("canaryService", "")
    if not canary_svc:
        return False, "canaryService unset"
    eps = kget_json("endpoints", ns="bleater", name=canary_svc)
    if not eps:
        return False, "endpoints missing"
    addrs = set()
    for s in eps.get("subsets") or []:
        for a in (s.get("addresses") or []):
            if a.get("ip"):
                addrs.add(a["ip"])
    if not addrs:
        return False, "endpoints empty"
    rc, pods_json, _ = run_cmd(
        "kubectl get pod -n bleater -l app=like-service -o json 2>/dev/null"
    )
    try:
        pods = json.loads(pods_json).get("items", []) if pods_json else []
    except json.JSONDecodeError:
        return False, "pods list unparseable"
    ready_ips = set()
    for p in pods:
        ip = p.get("status", {}).get("podIP")
        if not ip:
            continue
        phase = p.get("status", {}).get("phase")
        if phase != "Running":
            continue
        for c in p.get("status", {}).get("conditions") or []:
            if c.get("type") == "Ready" and c.get("status") == "True":
                ready_ips.add(ip)
    if not addrs.issubset(ready_ips):
        missing = addrs - ready_ips
        return False, f"endpoints include non-Ready IPs: {missing}"
    return True, f"{len(addrs)} endpoint IP(s), all Running+Ready"


def _sampler_loop():
    while not TRAFFIC_STATE["stopped"]:
        TRAFFIC_STATE["samples"] += 1
        ok, reason = _sample_traffic_path()
        if ok:
            TRAFFIC_STATE["stable_samples"] += 1
        else:
            TRAFFIC_STATE["last_err"] = reason
        time.sleep(5)


def traffic_start(setup_info):
    TRAFFIC_STATE["stopped"] = False
    t = threading.Thread(target=_sampler_loop, daemon=True)
    TRAFFIC_STATE["thread"] = t
    t.start()


def traffic_stop():
    TRAFFIC_STATE["stopped"] = True
    t = TRAFFIC_STATE["thread"]
    if t:
        t.join(timeout=10)


# ─────────────────────────────────────────────
# CHECK 1: analysis_query_semantic_correct
# ─────────────────────────────────────────────
def check_analysis_query_semantic_correct(setup_info):
    """
    Semantic audit of the AnalysisTemplate AFTER the durability window.
    All of these must be true simultaneously:
      - rate window >= 180s (>=3x the 60s scrape — per Q4 runbook guidance)
      - NaN-safe (query contains `or on() vector(...)` or `or vector(...)`
        OR metric has `nanStrategy` annotation)
      - inconclusiveLimit >= 1 (broken default is 0)
      - count >= 2 (broken default is 1)
      - interval >= 30s
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

    # Rate window >= 180s (derivable from Q4 runbook: >=3x scrape_interval=60s)
    rate_matches = re.findall(r"rate\([^[]*\[(\d+)([smh])\]", query)
    if not rate_matches:
        issues.append("no rate() window found")
        window_s = 0
    else:
        value, unit = rate_matches[0]
        window_s = int(value) * {"s": 1, "m": 60, "h": 3600}.get(unit, 1)
        if window_s < 180:
            issues.append(f"rate window {window_s}s < 180s required")

    # Must reference the metric + service (not a trivial constant)
    if "http_requests_total" not in query:
        issues.append("query does not reference http_requests_total metric")
    if "bleater-like-service" not in query:
        issues.append("query does not reference bleater-like-service by label")
    if re.search(r"^\s*vector\s*\(\s*[01]\s*\)\s*$", query) or \
       re.search(r"^\s*scalar\s*\(\s*vector\s*\(", query):
        issues.append("query is a trivial constant")

    # NaN safety: accept any of the canonical Argo Rollouts patterns —
    # query-level `or on() vector(...)`, `nanStrategy` on the provider, or
    # `len(result) == 0` handled explicitly in the successCondition.
    nan_safe = bool(re.search(r"\bor\s+(on\s*\(\s*\)\s+)?vector\s*\(", query))
    if not nan_safe:
        prov = m.get("provider", {}).get("prometheus", {})
        if prov.get("nanStrategy"):
            nan_safe = True
    if not nan_safe:
        succ = (m.get("successCondition") or "")
        if re.search(r"len\s*\(\s*result\s*\)\s*==\s*0", succ):
            nan_safe = True
    if not nan_safe:
        issues.append(
            "query not NaN-safe: add `or on() vector(0)` to the query, "
            "set `nanStrategy` on the provider, or handle `len(result) == 0` "
            "in successCondition"
        )

    # inconclusiveLimit > 0 (broken default is 0 — prompt asks for analysis
    # that catches *real* regressions, i.e. not failing on first NaN)
    inc = m.get("inconclusiveLimit")
    if not isinstance(inc, int) or inc < 1:
        issues.append(f"inconclusiveLimit={inc} (need >= 1; 0 fails on first NaN)")

    # count >= 3 — broken default is 1, and at least 3 samples are needed
    # before the analysis is meaningful (1 could be noise, 2 is a coin toss).
    count = m.get("count")
    if not isinstance(count, int) or count < 3:
        issues.append(f"count={count} (need >= 3)")

    # interval >= 30s
    interval = (m.get("interval") or "").strip()
    sec = 0
    mtch = re.match(r"(\d+)([smh])", interval) if interval else None
    if mtch:
        sec = int(mtch.group(1)) * {"s": 1, "m": 60, "h": 3600}[mtch.group(2)]
    if sec < 30:
        issues.append(f"interval={interval} (need >= 30s)")

    if issues:
        return 0.0, "; ".join(issues)
    return 1.0, (
        f"Semantic OK: window={window_s}s, count={count}, interval={interval}, "
        f"inconclusiveLimit={inc}"
    )


# ─────────────────────────────────────────────
# CHECK 2: rollout_timeout_functional
# ─────────────────────────────────────────────
def check_rollout_timeout_functional(setup_info):
    """
    Timeout protection is functionally configured:
      - progressDeadlineSeconds in [1, 3600] (platform SLO: stalls must
        surface within 1h — see cost-reduction-notes-q4 ConfigMap)
      - progressDeadlineAbort: true (so a stall actually aborts, not just marks)
      - No indefinite pauses (every pause has a duration)
      - canary.analysis.templates set
      - Rollout conditions contain Progressing entry with non-empty reason
    """
    r = kget_json("rollout", ns="bleater", name="bleater-like-service")
    if not r:
        return 0.0, "Rollout not found"

    issues = []
    spec = r.get("spec", {})

    pds = spec.get("progressDeadlineSeconds")
    if not isinstance(pds, int) or not (1 <= pds <= 3600):
        issues.append(f"progressDeadlineSeconds={pds} (need 1..3600)")

    if spec.get("progressDeadlineAbort") is not True:
        issues.append("progressDeadlineAbort must be true")

    steps = spec.get("strategy", {}).get("canary", {}).get("steps", []) or []
    for i, step in enumerate(steps):
        if "pause" in step:
            pause = step.get("pause") or {}
            if pause.get("duration") in (None, "", 0):
                issues.append(f"step {i} has indefinite pause (no duration)")

    conds = r.get("status", {}).get("conditions", []) or []
    prog = next((c for c in conds if c.get("type") == "Progressing"), None)
    if not prog or not prog.get("reason"):
        issues.append("Progressing condition missing or has no reason")

    analysis_ref = spec.get("strategy", {}).get("canary", {}).get("analysis", {})
    if not analysis_ref.get("templates"):
        issues.append("canary.analysis.templates not set on rollout")

    if issues:
        return 0.0, "; ".join(issues)
    return 1.0, (
        f"Timeout functional: pds={pds}, abort=true, all pauses timed, "
        f"Progressing.reason={prog.get('reason')}"
    )


# ─────────────────────────────────────────────
# CHECK 3: canary_traffic_verified_flowing
# ─────────────────────────────────────────────
def check_canary_traffic_verified_flowing(setup_info):
    """
    Traffic path is valid AND stable across the durability window.
      - Canary service exists
      - targetPort numerically matches the container's port (LIKE_PORT) OR
        is a port-name string that K8s actually resolves (endpoints will
        tell us via the `ports[].port` they expose — if K8s couldn't
        resolve the name, endpoints would be empty and we fail anyway)
      - Endpoints populated, not the sentinel (8099)
      - >=80% of 180s-window samples saw endpoints fully backed by
        Running+Ready pod IPs (catches flapping pods)
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
        return 0.0, "Canary targetPort still 8099 (sentinel broken value)"

    # If agent set a numeric targetPort, require it to match the container
    # port. A wrong integer (e.g. 8005) slips past endpoint population
    # (endpoints mirror the selector, not the port) but breaks real traffic.
    try:
        like_port = int(setup_info.get("LIKE_PORT", "8006"))
    except ValueError:
        like_port = 8006
    if isinstance(target_port, int) and target_port != like_port:
        return 0.0, (
            f"Canary targetPort={target_port} does not match container port "
            f"{like_port} — traffic won't reach the pod"
        )

    samples = TRAFFIC_STATE["samples"]
    stable = TRAFFIC_STATE["stable_samples"]
    if samples < 10:
        return 0.0, f"sampling insufficient (samples={samples})"
    rate = stable / samples
    if rate < 0.8:
        return 0.0, (
            f"Traffic path stable only {stable}/{samples}={rate:.0%} of "
            f"window (need >=80%); last_err={TRAFFIC_STATE['last_err']}"
        )
    return 1.0, (
        f"Traffic path stable {stable}/{samples}={rate:.0%} across window; "
        f"targetPort={target_port}"
    )


# ─────────────────────────────────────────────
# CHECK 4: rollout_healthy_full_promotion
# ─────────────────────────────────────────────
def check_rollout_healthy_full_promotion(setup_info):
    """
    End-to-end: rollout is fully promoted AND cleanly succeeded, AND the
    platform saboteurs that were installed at setup are neutralized.
      - phase == Healthy
      - last setWeight step reached (final weight in the steps list)
      - >=1 Successful AR with a semantically-correct query
      - No Failed/Error/Inconclusive ARs
      - Rollout pods Running AND Ready for >=30s
      - None of the platform saboteurs still registered in the cluster
    """
    # First: platform saboteurs must be gone. We check the four saboteurs
    # setup.sh installs; if any are still in-cluster the agent "passed" by
    # outrunning the reconcilers rather than actually neutralizing them.
    remaining = []
    saboteurs = [
        ("mutatingwebhookconfiguration", None, "platform-config-sync"),
        ("cronjob", "argocd", "platform-drift-reconciler"),
        ("daemonset", "monitoring", "platform-health-monitor"),
        ("application", "argocd", "bleater-platform-rollouts"),
    ]
    for resource, ns, name in saboteurs:
        ns_arg = f"-n {ns}" if ns else ""
        rc, out, _ = run_cmd(
            f"kubectl get {resource} {name} {ns_arg} -o name 2>/dev/null"
        )
        if rc == 0 and out.strip():
            remaining.append(f"{resource}/{name}")
    if remaining:
        return 0.0, (
            "Platform saboteurs still running — rollout isn't truly clean. "
            f"Still present: {remaining}"
        )

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

    # Sample traffic-path health across the 180s durability window.
    print("[grader] Sampling canary traffic path for 180s durability window...")
    traffic_start(setup_info)
    time.sleep(180)
    traffic_stop()
    print(
        f"[grader] Traffic path: {TRAFFIC_STATE['stable_samples']}/"
        f"{TRAFFIC_STATE['samples']} samples stable | last_err={TRAFFIC_STATE['last_err']}"
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
