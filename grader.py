#!/usr/bin/env python3
"""
Grader for argo-rollouts-canary-stuck task.

3 FUNCTIONAL subscores, equal weight 1/3 each. Each check verifies real
behavior, not config existence.

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
   - Rollout's Progressing condition is populated with a reason

3. rollout_healthy_full_promotion
   - At least 2-of-3 actively-reverting saboteurs are gone
   - Canary service targetPort != sentinel (8099) — config-level fault repair
   - Rollout phase = Healthy
   - currentStepIndex reached final setWeight step (promotion complete)
   - canary.analysis.templates wired on the rollout
   - At least one Successful AnalysisRun whose query is semantically correct
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
    Timeout protection is functionally configured. Strictly the
    *timeout-related* fields:
      - progressDeadlineSeconds in [1, 1800] (platform SLO: stalls must
        surface within 30m — see cost-reduction-notes-q4 ConfigMap)
      - progressDeadlineAbort: true (so a stall actually aborts, not just marks)
      - Rollout conditions contain a Progressing entry with non-empty reason
        (proves the controller is actively reconciling, not stuck)

    The pause-duration and analysis.templates assertions used to live here
    but they're really *canary strategy* concerns — they've moved to
    rollout_healthy_full_promotion where they belong.
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

    conds = r.get("status", {}).get("conditions", []) or []
    prog = next((c for c in conds if c.get("type") == "Progressing"), None)
    if not prog or not prog.get("reason"):
        issues.append("Progressing condition missing or has no reason")

    if issues:
        return 0.0, "; ".join(issues)
    return 1.0, (
        f"Timeout functional: pds={pds}, abort=true, "
        f"Progressing.reason={prog.get('reason')}"
    )

# ─────────────────────────────────────────────
# CHECK 3: rollout_healthy_full_promotion
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
    # Require 2-of-3 actively-reverting saboteurs to be gone. The MWC is
    # always neutralized; the cronjob and daemonset are the discriminators.
    # 2-of-3 (one tolerated) keeps fairness — agents who find the loud one
    # plus one of the two quieter ones still pass — while restoring the
    # variance the 1-of-3 relax had collapsed.
    # The ArgoCD Application is excluded (passive/zombie — doesn't actively
    # revert fixes and isn't called out in the prompt).
    remaining = []
    saboteurs = [
        ("mutatingwebhookconfiguration", None, "platform-config-sync"),
        ("cronjob", "argocd", "platform-drift-reconciler"),
        ("daemonset", "monitoring", "platform-health-monitor"),
    ]
    for resource, ns, name in saboteurs:
        ns_arg = f"-n {ns}" if ns else ""
        rc, out, _ = run_cmd(
            f"kubectl get {resource} {name} {ns_arg} -o name 2>/dev/null"
        )
        if rc == 0 and out.strip():
            remaining.append(f"{resource}/{name}")
    if len(remaining) >= 2:
        return 0.0, (
            f"Too many platform saboteurs still running "
            f"({len(remaining)}/3): {remaining}. "
            f"Need at least 2 of 3 neutralized."
        )

    r = kget_json("rollout", ns="bleater", name="bleater-like-service")
    if not r:
        return 0.0, "Rollout not found"

    # Config-level check that the canary service's targetPort sentinel
    # fault was reverted. We don't run a live traffic probe (image-port
    # introspection is out of scope for the harness) but an agent who
    # left the sentinel in place clearly didn't complete the task.
    canary_svc_name = r.get("spec", {}).get("strategy", {}).get("canary", {}).get("canaryService", "")
    if canary_svc_name:
        svc = kget_json("service", ns="bleater", name=canary_svc_name)
        if svc:
            svc_ports = svc.get("spec", {}).get("ports", []) or []
            if svc_ports and svc_ports[0].get("targetPort") == 8099:
                return 0.0, (
                    f"Canary service {canary_svc_name} still has the "
                    f"sentinel targetPort=8099 — fault not repaired"
                )

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

    # Canary-strategy hygiene: the rollout must reference at least one
    # AnalysisTemplate. (Indefinite-pause assertion removed — it was a
    # niche stylistic preference that uniformly blocked agents.)
    canary_spec = r.get("spec", {}).get("strategy", {}).get("canary", {}) or {}
    if not canary_spec.get("analysis", {}).get("templates"):
        return 0.0, "canary.analysis.templates not set on rollout — rollout has no AR-driven gating"

    # AR completion poll: an AnalysisRun with the semantically-correct
    # template (count>=3 + interval>=30s + rate window>=180s) takes ~90s
    # to finish. Rollout Healthy can be reported before the background
    # AR transitions to Successful — give the AR up to 90s extra to
    # settle before failing the agent on a timing race.
    for _ in range(18):
        ars_poll = kget_json("analysisrun", ns="bleater") or {}
        if any(
            (a.get("status", {}).get("phase") or "").lower() == "successful"
            for a in ars_poll.get("items", []) or []
        ):
            break
        time.sleep(5)

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

    # 180s durability window lets lingering reconcilers take their shot at
    # reverting the agent's fixes before we grade — so a "pass" means the
    # fix survived real-world adversarial conditions, not a single snapshot.
    print("[grader] Durability window (180s) — letting reconcilers try to revert...")
    time.sleep(180)

    checks = {
        "analysis_query_semantic_correct": check_analysis_query_semantic_correct,
        "rollout_timeout_functional": check_rollout_timeout_functional,
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
