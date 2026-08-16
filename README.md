# hello-app

A deliberately small application repository used to prove the Solo VPS delivery path end to end.

The repository is intentionally simple: application code lives in `examples/hello-app`, GitHub Actions builds and publishes an immutable GHCR image, verifies that exact digest in a fresh job, and then deploys the same digest to an already configured Coolify Docker Image resource through the restricted Solo VPS SSH/API transport.

## Repository layout

```text
.github/workflows/ci.yml         application CI, GHCR publish, digest verification, Coolify deploy
examples/hello-app/             dependency-free Python HTTP fixture and Dockerfile
scripts/coolify_deploy_api.py   constrained Coolify deployment transaction with automatic image rollback
scripts/validate_coolify_image_handoff.py
                                immutable GHCR/Coolify image-field validator
tests/                          offline contract tests for the deployment helpers
```

## Local tests

From the repository root:

```bash
python3 -m unittest discover -s examples/hello-app -p 'test_*.py'
python3 -m unittest discover -s tests -p 'test_*.py'
```

The application contract is:

```text
GET /        -> 200 {"service":"solo-vps-hello","status":"ok"}
GET /healthz -> 200 {"status":"ok"}
container    -> non-root UID/GID 65532
port         -> 8080 inside the container
```

## CI/CD behavior

Pull requests run the application/helper tests and build the Docker image without publishing it.

A push to `main` performs the maintained production path:

```text
tests
-> build + publish to GHCR
-> immutable repository@sha256:digest output
-> fresh-job pull + /healthz smoke test of that exact digest
-> production environment gate
-> restricted SSH tunnel to loopback-only Coolify API
-> read-only API preflight
-> exact-digest deployment
-> require running:healthy
```

If the candidate deployment fails after desired-state mutation, the deployment helper restores the exact previous immutable digest and requires it to return to `running:healthy`. A successful automatic rollback still makes the attempted release job fail with `DEPLOY_FAILED_ROLLBACK_OK`; a failed rollback reports `DEPLOY_FAILED_ROLLBACK_FAILED` plus a token-free known-good recovery command.

This rollback covers the application image desired state only. It does not reverse database migrations, destructive data changes, queue/event effects, or external side effects.

## Required GitHub `production` environment

Secrets:

```text
SOLO_VPS_DEPLOY_SSH_KEY
COOLIFY_API_TOKEN
```

Variables:

```text
SOLO_VPS_DEPLOY_HOST
SOLO_VPS_SSH_KNOWN_HOSTS
SOLO_VPS_DEPLOY_SSH_FINGERPRINT
COOLIFY_RESOURCE_UUID
```

Keep the Coolify management API loopback-only. Do not publish port `8000` as a CI shortcut and do not store deployment private keys or Coolify API tokens in repository files.
