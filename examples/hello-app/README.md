# Solo VPS hello application

This is a deliberately small application fixture for M10/M11 validation.

Contract:

- dependency-free Python runtime code;
- listens on container port `8080`;
- `GET /` returns a small JSON response;
- `GET /healthz` returns HTTP `200` and `{"status":"ok"}`;
- Docker image runs as numeric non-root UID/GID `65532`;
- the Dockerfile defines the health check;
- there is no Compose file and no host-port publication in this example.

Run the local application tests from the repository root:

```bash
make test-example-app
```

Build/run validation is intentionally separate because Docker is not assumed to exist on every controller:

```bash
docker build -t solo-vps-hello:dev examples/hello-app
docker run --rm -p 127.0.0.1:18080:8080 solo-vps-hello:dev
curl --fail http://127.0.0.1:18080/healthz
```

The loopback mapping above is only for local testing. A normal Coolify domain deployment should expose container port `8080` to the platform proxy rather than publish an application host port.


## CI template

M11 uses this fixture as the concrete build/test target for [`../../templates/github-actions/hello-app-ci.yml`](../../templates/github-actions/hello-app-ci.yml).

The workflow runs these HTTP tests on pull requests, builds the Dockerfile without publishing, and publishes the main-branch image to GHCR with an immutable digest output. It deliberately does not deploy to Coolify yet; that downstream interface remains blocked by the M9 architecture/install decision.

## Base image update policy

The Dockerfile uses an explicit Python patch tag (`3.13.14-slim-bookworm`). The project-level Dependabot configuration checks this directory weekly for patch updates within the current Python feature series, while M11 Buildx jobs use `pull: true` to refresh the referenced image. See [`../../docs/dependency-hygiene.md`](../../docs/dependency-hygiene.md) for the current digest/rebuild trade-off and validation contract.
