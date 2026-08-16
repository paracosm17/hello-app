# Solo VPS hello application

This directory contains the deliberately small application fixture used for real Solo VPS CI/CD, rollback, terminal, live-log and retained-log proofs.

Contract:

- dependency-free Python runtime code;
- listens on container port `8080`;
- `GET /` returns `{"service":"solo-vps-hello","status":"ok"}`;
- `GET /healthz` returns HTTP `200` and `{"status":"ok"}`;
- Docker image runs as numeric non-root UID/GID `65532`;
- the Dockerfile defines the health check;
- there is no Compose file and no application host-port publication in the repository.

Run the local application tests from the repository root:

```bash
python3 -m unittest discover -s examples/hello-app -p 'test_*.py'
```

Build/run validation is separate:

```bash
docker build -t solo-vps-hello:dev examples/hello-app
docker run --rm -p 127.0.0.1:18080:8080 solo-vps-hello:dev
curl --fail http://127.0.0.1:18080/healthz
```

The loopback mapping above is only for local testing. Coolify exposes container port `8080` to its platform proxy rather than publishing an application host port.

## CI delivery contract

The repository workflow in `.github/workflows/ci.yml` runs tests and a non-publishing image build for pull requests. A push to `main` publishes to GHCR, verifies the exact immutable digest in a fresh job, and deploys that same digest to the configured Coolify Docker Image resource.

The deployment helper preserves the previous immutable desired image before mutation. Failed, cancelled, unhealthy or timed-out candidate deployments trigger an automatic exact-digest rollback and require the restored application to become `running:healthy` again.

See the repository-level `README.md` for the GitHub `production` environment contract and the image-rollback boundary.
