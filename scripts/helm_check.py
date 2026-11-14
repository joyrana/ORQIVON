"""Render the Helm chart under several value sets and validate what comes out.

For each scenario the chart is rendered (with `helm template` when helm is installed, otherwise
with tools/helm-render, which uses the same Go template engine), every manifest is validated
against the Kubernetes OpenAPI schema for the oldest and newest supported versions, and the
platform's deployment policy is checked: least privilege, no secrets in manifests, probes,
resources, and the routing the app depends on. Invalid value sets must fail to render.

    python scripts/helm_check.py            # all scenarios
    python scripts/helm_check.py --print default
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import kubernetes_validate
import yaml

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "infrastructure" / "helm" / "agentic-sdlc"
RENDERER = ROOT / "tools" / "helm-render"
KUBE_VERSIONS = ("1.30.0", "1.33.0")
CRDS = {"ServiceMonitor"}  # not in the core schema; checked structurally below
SECRETISH = re.compile(
    r"(SECRET|TOKEN|PASSWORD|API_KEY|PRIVATE_KEY|DATABASE_URL)", re.IGNORECASE
)

SCENARIOS: dict[str, dict[str, Any]] = {
    "default": {},
    "gvisor-docker": {
        "worker": {"sandboxBackend": "docker", "runtimeClassName": "gvisor"},
        "workerSecret": "asw-worker-credentials",
        "metrics": {"serviceMonitor": {"enabled": True}},
        "config": {
            "identity": "providers:\n  - id: corp\n    issuer: https://login.example.com\n",
            "gitConnections": "connections:\n  - name: gh\n    kind: github\n",
            "models": "endpoints: {}\n",
        },
        "imagePullSecrets": [{"name": "registry"}],
        "backup": {"enabled": True, "retentionDays": 30},
    },
    "single-node": {
        "api": {"replicas": 1},
        "worker": {"replicas": 1},
        "persistence": {"accessMode": "ReadWriteOnce", "storageClass": "standard"},
        "ingress": {"enabled": False},
        "networkPolicy": {"enabled": False},
        "migrations": {"enabled": False},
    },
}
# Value sets that would install but not work: rendering must refuse them.
INVALID: dict[str, tuple[dict[str, Any], str]] = {
    "rwo-shared": ({"persistence": {"accessMode": "ReadWriteOnce"}}, "ReadWriteOnce"),
    "no-persistence-ha": (
        {"persistence": {"enabled": False}},
        "persistence.enabled=false",
    ),
    "http-in-production": (
        {"publicBaseUrl": "http://asw.example.com"},
        "must be https",
    ),
    "no-public-url": ({"publicBaseUrl": ""}, "publicBaseUrl is required"),
    "bad-sandbox": ({"worker": {"sandboxBackend": "none"}}, "sandboxBackend must be"),
}


def merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = (
            merge(out[k], v)
            if isinstance(v, dict) and isinstance(out.get(k), dict)
            else v
        )
    return out


def render(overrides: dict[str, Any]) -> tuple[int, str, str]:
    if shutil.which("helm"):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
            yaml.safe_dump(overrides, f)
            f.flush()
            p = subprocess.run(
                [
                    "helm",
                    "template",
                    "asw",
                    str(CHART),
                    "-n",
                    "agentic-sdlc",
                    "-f",
                    f.name,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        return p.returncode, p.stdout, p.stderr
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    values = merge(yaml.safe_load((CHART / "values.yaml").read_text()), overrides)
    ctx = {
        "Values": values,
        "Chart": {
            "Name": chart["name"],
            "Version": chart["version"],
            "AppVersion": chart["appVersion"],
        },
        "Release": {"Name": "asw", "Namespace": "agentic-sdlc", "Service": "Helm"},
    }
    p = subprocess.run(
        ["go", "run", ".", str(CHART)],
        cwd=RENDERER,
        input=json.dumps(ctx),
        capture_output=True,
        text=True,
        check=False,
    )
    return p.returncode, p.stdout, p.stderr


def pods(docs: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for d in docs:
        if d["kind"] in {"Deployment", "Job"}:
            out.append((d["metadata"]["name"], d["spec"]["template"]["spec"]))
        elif d["kind"] == "CronJob":
            out.append(
                (
                    d["metadata"]["name"],
                    d["spec"]["jobTemplate"]["spec"]["template"]["spec"],
                )
            )
    return out


def policy(name: str, docs: list[dict[str, Any]], values: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    kinds = {(d["kind"], d["metadata"]["name"]) for d in docs}
    for pod_name, spec in pods(docs):
        if spec.get("automountServiceAccountToken") is not False:
            errors.append(f"{pod_name}: service account token is mounted")
        if spec.get("hostNetwork") or spec.get("hostPID") or spec.get("hostIPC"):
            errors.append(f"{pod_name}: host namespaces")
        for c in spec["containers"]:
            sc = c.get("securityContext", {})
            where = f"{pod_name}/{c['name']}"
            if sc.get("privileged"):
                errors.append(f"{where}: privileged")
            if sc.get("capabilities", {}).get("drop") != ["ALL"]:
                errors.append(f"{where}: capabilities not dropped")
            if not sc.get("readOnlyRootFilesystem"):
                errors.append(f"{where}: writable root filesystem")
            if not c.get("resources", {}).get("limits", {}).get("memory"):
                errors.append(f"{where}: no memory limit")
            for env in c.get("env", []):
                if SECRETISH.search(env["name"]) and "value" in env:
                    errors.append(
                        f"{where}: secret-like variable {env['name']} set in the manifest"
                    )
            if pod_name.endswith(("-api", "-worker")):
                mounts = {m["mountPath"] for m in c.get("volumeMounts", [])}
                if not {"/data", "/tmp", "/var/tmp"} <= mounts:
                    errors.append(f"{where}: read-only root needs writable /data, /tmp and /var/tmp")
            is_worker = pod_name.endswith("-worker")
            if is_worker:
                added = set(sc.get("capabilities", {}).get("add", []))
                if values["worker"]["sandboxBackend"] == "docker" and added:
                    errors.append(
                        f"{where}: docker backend needs no added capabilities"
                    )
                if spec.get("runtimeClassName") != (
                    values["worker"]["runtimeClassName"] or None
                ):
                    errors.append(f"{where}: runtimeClassName not applied")
                secrets = [e["secretRef"]["name"] for e in c.get("envFrom", [])]
                if values["workerSecret"] and values["workerSecret"] not in secrets:
                    errors.append(f"{where}: workerSecret not given to the worker")
                if "livenessProbe" not in c:
                    errors.append(f"{where}: no liveness probe")
            else:
                pod_sc = spec.get("securityContext", {})
                if not (pod_sc.get("runAsNonRoot") and pod_sc.get("runAsUser", 0) > 0):
                    errors.append(f"{where}: may run as root")
                if sc.get("allowPrivilegeEscalation") is not False:
                    errors.append(f"{where}: privilege escalation allowed")
                if pod_name.endswith(("-api", "-web")) and not {
                    "readinessProbe",
                    "livenessProbe",
                } <= set(c):
                    errors.append(f"{where}: missing probes")
                if values["workerSecret"] and any(
                    e["secretRef"]["name"] == values["workerSecret"]
                    for e in c.get("envFrom", [])
                ):
                    errors.append(f"{where}: worker-only secret exposed")
    api = next(
        d
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-api")
    )
    api_c = api["spec"]["template"]["spec"]["containers"][0]
    if api_c["readinessProbe"]["httpGet"]["path"] != "/api/v1/ready":
        errors.append("api: readiness must use /api/v1/ready")
    env = {e["name"]: e.get("value") for e in api_c["env"]}
    for key, file_ in (
        ("identity", "IDENTITY_CONFIG_FILE"),
        ("gitConnections", "GIT_CONNECTIONS_FILE"),
    ):
        if bool(values["config"][key]) != (file_ in env):
            errors.append(f"api: {file_} wiring does not match config.{key}")
    cm = next(d for d in docs if d["kind"] == "ConfigMap")
    for key, f in (
        ("identity", "identity.yaml"),
        ("gitConnections", "git-connections.yaml"),
    ):
        if values["config"][key] and yaml.safe_load(cm["data"][f]) != yaml.safe_load(
            values["config"][key]
        ):
            errors.append(f"configmap: {f} content altered")
    if values["ingress"]["enabled"]:
        ing = next(d for d in docs if d["kind"] == "Ingress")
        routes = {
            p["path"]: p["backend"]["service"]["name"]
            for p in ing["spec"]["rules"][0]["http"]["paths"]
        }
        if not (routes["/api"].endswith("-api") and routes["/scim"].endswith("-api")):
            errors.append("ingress: /api and /scim must reach the API")
        if not routes["/"].endswith("-web"):
            errors.append("ingress: / must reach the web app")
    if values["networkPolicy"]["enabled"]:
        worker_np = [
            d
            for d in docs
            if d["kind"] == "NetworkPolicy"
            and d["metadata"]["name"].endswith("-worker")
        ]
        if not worker_np or worker_np[0]["spec"]["ingress"] != []:
            errors.append("networkpolicy: the worker must accept no inbound traffic")
    expected = {
        ("Job", "asw-agentic-sdlc-migrate"): values["migrations"]["enabled"],
        ("ServiceMonitor", "asw-agentic-sdlc"): values["metrics"]["serviceMonitor"][
            "enabled"
        ],
        ("PersistentVolumeClaim", "asw-agentic-sdlc-data"): values["persistence"][
            "enabled"
        ],
        ("CronJob", "asw-agentic-sdlc-backup"): values["backup"]["enabled"],
    }
    for d in docs:
        if d["kind"] == "CronJob":
            c = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
            if f"-mtime +{values['backup']['retentionDays']} " not in c["command"][-1]:
                errors.append("backup: retention not applied")
            if not any(
                m["mountPath"] == "/data" and m.get("readOnly")
                for m in c["volumeMounts"]
            ):
                errors.append("backup: must mount the data directory read-only")
    for k, want in expected.items():
        if (k in kinds) != want:
            errors.append(f"{k[0]} {k[1]}: {'missing' if want else 'unexpected'}")
    pvc = [d for d in docs if d["kind"] == "PersistentVolumeClaim"]
    if (
        pvc
        and pvc[0]["metadata"].get("annotations", {}).get("helm.sh/resource-policy")
        != "keep"
    ):
        errors.append("pvc: must survive helm uninstall")
    return [f"{name}: {e}" for e in errors]


def check(name: str, overrides: dict[str, Any], show: bool) -> list[str]:
    code, out, err = render(overrides)
    if code != 0:
        return [f"{name}: render failed: {err.strip()}"]
    if show:
        print(out)
    docs = [d for d in yaml.safe_load_all(out) if d]
    errors: list[str] = []
    for d in docs:
        if d["kind"] in CRDS:
            continue
        for version in KUBE_VERSIONS:
            try:
                kubernetes_validate.validate(d, version, strict=True)
            except kubernetes_validate.ValidationError as e:
                errors.append(
                    f"{name}: {d['kind']} {d['metadata']['name']} (k8s {version}): {e.message}"
                )
    values = merge(yaml.safe_load((CHART / "values.yaml").read_text()), overrides)
    errors += policy(name, docs, values)
    print(f"{'FAIL' if errors else 'ok  '} {name}: {len(docs)} manifests")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--print", dest="show", metavar="SCENARIO")
    args = parser.parse_args()
    errors: list[str] = []
    for name, overrides in SCENARIOS.items():
        if args.show and args.show != name:
            continue
        errors += check(name, overrides, args.show == name)
    if not args.show:
        for name, (overrides, message) in INVALID.items():
            code, _, err = render(overrides)
            ok = code != 0 and message in err
            print(f"{'ok  ' if ok else 'FAIL'} refuses {name}")
            if not ok:
                errors.append(
                    f"{name}: expected a render error containing {message!r}, got: {err.strip()}"
                )
    for e in errors:
        print(e, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
