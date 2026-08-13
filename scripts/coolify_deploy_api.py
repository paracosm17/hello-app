#!/usr/bin/env python3
"""Safely drive one immutable Docker Image deployment through loopback Coolify API.

The script is intentionally constrained to the Solo VPS management contract:
- Coolify API must remain loopback-only; direct target access is 127.0.0.1:8000;
- the only alternate client endpoint is the fixed runner-side SSH tunnel at 127.0.0.1:18000;
- the target must already be a Docker Image application;
- the image identity must be an immutable public GHCR sha256 digest;
- mutation requires an explicit --apply flag, confirmation environment value, and bearer token supplied out of band.

This is an M11 control-plane proof helper, not a general Coolify client.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

try:
    from scripts.validate_coolify_image_handoff import CoolifyImageHandoff, parse_image_ref
except ModuleNotFoundError:  # Direct execution as scripts/coolify_deploy_api.py
    from validate_coolify_image_handoff import CoolifyImageHandoff, parse_image_ref

DEFAULT_BASE_URL = "http://127.0.0.1:8000/api/v1"
SSH_TUNNEL_BASE_URL = "http://127.0.0.1:18000/api/v1"
APPLY_CONFIRMATION = "I_HAVE_REVIEWED_THE_LOOPBACK_COOLIFY_API_DEPLOYMENT"
_ALLOWED_BASE_URLS = {DEFAULT_BASE_URL, SSH_TUNNEL_BASE_URL}
_RESOURCE_UUID_RE = re.compile(r"^[a-z0-9]{8,64}$")
_TERMINAL_DEPLOYMENT_STATUSES = {"finished", "failed", "cancelled-by-user"}


class CoolifyDeployError(RuntimeError):
    """Fail-closed error for an unsafe or unsuccessful deployment operation."""


@dataclass(frozen=True)
class DeploymentPlan:
    base_url: str
    resource_uuid: str
    handoff: CoolifyImageHandoff
    expected_port: str
    allow_domain: bool

    @property
    def application_url(self) -> str:
        return f"{self.base_url}/applications/{quote(self.resource_uuid, safe='')}"

    @property
    def start_url(self) -> str:
        return f"{self.application_url}/start"

    @property
    def update_payload(self) -> dict[str, str]:
        # The owner-target v4.1.2 proof established that this General-form pair
        # reconstructs repository@sha256:<digest> without the creation-UI double marker.
        return {
            "docker_registry_image_name": self.handoff.preferred_ui_image_name,
            "docker_registry_image_tag": self.handoff.preferred_ui_image_tag,
        }

    def deployment_url(self, deployment_uuid: str) -> str:
        return f"{self.base_url}/deployments/{quote(deployment_uuid, safe='')}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "resource_uuid": self.resource_uuid,
            "image_ref": self.handoff.image_ref,
            "expected_port": self.expected_port,
            "allow_domain": self.allow_domain,
            "application_url": self.application_url,
            "start_url": self.start_url,
            "update_payload": self.update_payload,
            "required_token_permissions": ["read", "write", "deploy"],
        }


def normalize_base_url(base_url: str) -> str:
    if base_url != base_url.strip():
        raise CoolifyDeployError("Coolify API base URL must not contain surrounding whitespace")
    normalized = base_url.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise CoolifyDeployError(
            "Coolify API proof must stay on loopback HTTP; never expose or call the management API externally"
        )
    if parsed.path != "/api/v1" or parsed.params or parsed.query or parsed.fragment:
        raise CoolifyDeployError("Coolify API base URL must end exactly at /api/v1")
    if normalized not in _ALLOWED_BASE_URLS:
        raise CoolifyDeployError(
            "Coolify API base URL must be exactly the direct 127.0.0.1:8000 endpoint "
            "or the fixed runner-side 127.0.0.1:18000 SSH-tunnel endpoint"
        )
    return normalized


def build_plan(
    *,
    base_url: str,
    resource_uuid: str,
    image_ref: str,
    expected_port: str = "8080",
    allow_domain: bool = False,
) -> DeploymentPlan:
    if not _RESOURCE_UUID_RE.fullmatch(resource_uuid):
        raise CoolifyDeployError("resource UUID must contain only lowercase letters/digits and be 8-64 characters")
    if not expected_port.isdigit() or not 1 <= int(expected_port) <= 65535:
        raise CoolifyDeployError("expected port must be an integer from 1 to 65535")
    try:
        handoff = parse_image_ref(image_ref)
    except ValueError as exc:
        raise CoolifyDeployError(str(exc)) from exc
    return DeploymentPlan(
        base_url=normalize_base_url(base_url),
        resource_uuid=resource_uuid,
        handoff=handoff,
        expected_port=expected_port,
        allow_domain=allow_domain,
    )


def _clean_optional(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def validate_application_state(application: dict[str, Any], plan: DeploymentPlan) -> None:
    if _clean_optional(application.get("uuid")) != plan.resource_uuid:
        raise CoolifyDeployError("Coolify API returned a different application UUID")
    if _clean_optional(application.get("build_pack")) != "dockerimage":
        raise CoolifyDeployError("target application must already use build_pack=dockerimage")

    ports = [part.strip() for part in _clean_optional(application.get("ports_exposes")).split(",") if part.strip()]
    if plan.expected_port not in ports:
        raise CoolifyDeployError(f"target application must expose internal port {plan.expected_port}")

    if _clean_optional(application.get("ports_mappings")):
        raise CoolifyDeployError("target application must not have host port mappings")

    if not plan.allow_domain and _clean_optional(application.get("fqdn")):
        raise CoolifyDeployError("isolated API proof requires an application with no public domain")

    current_name = _clean_optional(application.get("docker_registry_image_name"))
    if current_name and current_name != plan.handoff.preferred_ui_image_name:
        raise CoolifyDeployError(
            "target Docker Image repository differs from the requested immutable artifact repository"
        )


def validate_persisted_image_state(application: dict[str, Any], plan: DeploymentPlan) -> None:
    validate_application_state(application, plan)
    expected_name = plan.handoff.preferred_ui_image_name
    expected_tag = plan.handoff.preferred_ui_image_tag
    if _clean_optional(application.get("docker_registry_image_name")) != expected_name:
        raise CoolifyDeployError("Coolify did not persist the expected Docker Image repository")
    if _clean_optional(application.get("docker_registry_image_tag")) != expected_tag:
        raise CoolifyDeployError("Coolify did not persist the expected immutable sha256 tag/hash field")


class CoolifyApiClient:
    def __init__(self, token: str, *, timeout: float = 10.0, opener: Callable[..., Any] = urlopen):
        if not token or token != token.strip():
            raise CoolifyDeployError("Coolify API token is empty or contains surrounding whitespace")
        self._token = token
        self._timeout = timeout
        self._opener = opener

    def request(self, method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise CoolifyDeployError(f"Coolify API {method} failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise CoolifyDeployError(f"Coolify API {method} failed: {exc.reason}") from exc
        except OSError as exc:
            raise CoolifyDeployError(f"Coolify API {method} failed: {exc}") from exc

        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CoolifyDeployError(f"Coolify API {method} returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise CoolifyDeployError(f"Coolify API {method} returned a non-object response")
        return parsed


def require_apply_confirmation() -> None:
    confirmation = os.environ.get("COOLIFY_API_DEPLOY_CONFIRM", "")
    if confirmation != APPLY_CONFIRMATION:
        raise CoolifyDeployError(
            "--apply requires COOLIFY_API_DEPLOY_CONFIRM=" + APPLY_CONFIRMATION
        )


def load_token_from_environment() -> str:
    token = os.environ.get("COOLIFY_API_TOKEN", "")
    if not token:
        raise CoolifyDeployError(
            "COOLIFY_API_TOKEN is required for --check/--apply; read it interactively so it is not stored in shell history"
        )
    return token


def check_application(client: CoolifyApiClient, plan: DeploymentPlan) -> dict[str, Any]:
    application = client.request("GET", plan.application_url)
    validate_application_state(application, plan)
    return application


def apply_deployment(
    client: CoolifyApiClient,
    plan: DeploymentPlan,
    *,
    poll_interval: float = 2.0,
    poll_timeout: float = 180.0,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    before = check_application(client, plan)

    update_response = client.request("PATCH", plan.application_url, plan.update_payload)
    if _clean_optional(update_response.get("uuid")) != plan.resource_uuid:
        raise CoolifyDeployError("Coolify update response did not confirm the target application UUID")

    persisted = client.request("GET", plan.application_url)
    validate_persisted_image_state(persisted, plan)

    start_response = client.request("POST", plan.start_url)
    deployment_uuid = _clean_optional(start_response.get("deployment_uuid"))
    if not deployment_uuid or not _RESOURCE_UUID_RE.fullmatch(deployment_uuid):
        raise CoolifyDeployError("Coolify start response did not include a valid deployment_uuid")

    deadline = monotonic() + poll_timeout
    deployment: dict[str, Any] = {}
    while True:
        deployment = client.request("GET", plan.deployment_url(deployment_uuid))
        status = _clean_optional(deployment.get("status"))
        if status in _TERMINAL_DEPLOYMENT_STATUSES:
            break
        if status not in {"queued", "in_progress"}:
            raise CoolifyDeployError(f"unexpected Coolify deployment status: {status or '<empty>'}")
        if monotonic() >= deadline:
            raise CoolifyDeployError(
                f"timed out waiting for deployment {deployment_uuid}; last status={status}"
            )
        sleeper(poll_interval)

    if _clean_optional(deployment.get("status")) != "finished":
        raise CoolifyDeployError(
            f"Coolify deployment {deployment_uuid} ended with status={_clean_optional(deployment.get('status'))}"
        )

    health_deadline = monotonic() + poll_timeout
    app_status = ""
    while True:
        after = client.request("GET", plan.application_url)
        validate_persisted_image_state(after, plan)
        app_status = _clean_optional(after.get("status"))
        if app_status == "running:healthy":
            break
        if app_status.startswith(("exited", "stopped", "dead", "failed")) or app_status == "running:unhealthy":
            raise CoolifyDeployError(
                f"application did not become healthy after finished deployment: status={app_status}"
            )
        if monotonic() >= health_deadline:
            raise CoolifyDeployError(
                f"timed out waiting for application health after deployment {deployment_uuid}; "
                f"last status={app_status or '<empty>'}"
            )
        sleeper(poll_interval)

    return {
        "resource_uuid": plan.resource_uuid,
        "deployment_uuid": deployment_uuid,
        "image_ref": plan.handoff.image_ref,
        "deployment_status": "finished",
        "application_status": app_status or "not-reported",
        "previous_image_name": _clean_optional(before.get("docker_registry_image_name")),
        "previous_image_tag": _clean_optional(before.get("docker_registry_image_tag")),
    }


def _print_plan(plan: DeploymentPlan, as_json: bool) -> None:
    if as_json:
        print(json.dumps(plan.as_dict(), indent=2, sort_keys=True))
        return
    print("PASS Coolify loopback deploy API plan")
    print(f"  base_url: {plan.base_url}")
    print(f"  resource_uuid: {plan.resource_uuid}")
    print(f"  image_ref: {plan.handoff.image_ref}")
    print(f"  expected_internal_port: {plan.expected_port}")
    print(f"  public_domain_allowed: {'yes' if plan.allow_domain else 'no'}")
    print("  required_token_permissions: read, write, deploy")
    print("  update_payload:")
    print(json.dumps(plan.update_payload, indent=4, sort_keys=True))
    print("  mutation: disabled unless --apply is supplied")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plan/check/apply an immutable GHCR deployment through the loopback-only Coolify v4.1.2 API."
    )
    parser.add_argument("--resource-uuid", required=True)
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--expected-port", default="8080")
    parser.add_argument(
        "--allow-domain",
        action="store_true",
        help="Permit a target with an fqdn; default fails closed for the isolated M11 proof resource.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Read and validate the target application without mutation.")
    mode.add_argument("--apply", action="store_true", help="PATCH exact digest, start deployment, and poll to completion.")
    parser.add_argument("--poll-timeout", type=float, default=180.0)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        if args.poll_timeout <= 0 or args.poll_interval < 0:
            raise CoolifyDeployError("poll timeout must be > 0 and poll interval must be >= 0")
        plan = build_plan(
            base_url=args.base_url,
            resource_uuid=args.resource_uuid,
            image_ref=args.image_ref,
            expected_port=args.expected_port,
            allow_domain=args.allow_domain,
        )
        if not args.check and not args.apply:
            _print_plan(plan, args.json)
            return 0

        if args.apply:
            require_apply_confirmation()
        token = load_token_from_environment()
        client = CoolifyApiClient(token)
        if args.check:
            application = check_application(client, plan)
            evidence = {
                "status": "PASS",
                "mode": "check",
                "resource_uuid": plan.resource_uuid,
                "build_pack": _clean_optional(application.get("build_pack")),
                "ports_exposes": _clean_optional(application.get("ports_exposes")),
                "ports_mappings": _clean_optional(application.get("ports_mappings")),
                "fqdn": _clean_optional(application.get("fqdn")),
                "docker_registry_image_name": _clean_optional(application.get("docker_registry_image_name")),
                "docker_registry_image_tag": _clean_optional(application.get("docker_registry_image_tag")),
                "application_status": _clean_optional(application.get("status")),
            }
        else:
            evidence = {"status": "PASS", "mode": "apply"}
            evidence.update(
                apply_deployment(
                    client,
                    plan,
                    poll_interval=args.poll_interval,
                    poll_timeout=args.poll_timeout,
                )
            )

        if args.json:
            print(json.dumps(evidence, indent=2, sort_keys=True))
        else:
            label = "check" if args.check else "deployment"
            print(f"PASS Coolify loopback deploy API {label}")
            for key, value in evidence.items():
                if key not in {"status", "mode"}:
                    print(f"  {key}: {value}")
        return 0
    except CoolifyDeployError as exc:
        print(f"ERROR Coolify loopback deploy API: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
