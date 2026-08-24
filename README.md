# DevOpsPipeline

![CI](https://img.shields.io/badge/CI-GitHub_Actions-2088FF?logo=githubactions&logoColor=white)
![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)
![Code style](https://img.shields.io/badge/code%20style-ruff-261230?logo=ruff&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)
![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)

A production-grade, extensible CI/CD platform written in Python. DevOpsPipeline
orchestrates multi-stage build/test/deploy workflows across local machines,
Docker hosts, and Kubernetes clusters — with pluggable SCM integrations,
encrypted secret management, artifact retention, Prometheus metrics, and a
built-in FastAPI dashboard.

## Features

- **Declarative pipelines** — define stages, dependencies, and triggers in YAML
- **Dependency-graph orchestration** — automatic topological sorting, parallel
  fan-out of independent stages, and cascade skipping of downstream work
- **Pluggable runners** — execute jobs locally, in Docker containers, or as
  Kubernetes pods behind one uniform interface
- **Rich stage library** — checkout, build (make/npm/cargo/docker), test,
  lint, deploy (AWS/GCP/Azure/self-hosted), and notifications out of the box
- **Plugin system** — hooks for pipeline lifecycle events plus native GitHub,
  GitLab, Slack, and container-registry integrations
- **Security first** — Fernet-encrypted secret vault, OIDC token validation,
  RBAC with protected branches, and a queryable audit log
- **Artifacts** — local/S3 storage with SHA-256 integrity and retention
  policies that honor protected tags
- **Observability** — Prometheus counters/histograms and generated Grafana
  dashboards
- **Dashboard & API** — FastAPI backend exposing pipelines, runs, logs, and
  webhook ingestion
- **CLI** — a friendly Click-based `devops` command for everyday operations

## Architecture

```
                       ┌──────────────────────────────────────────┐
   webhook/schedule ──▶ │  Trigger Manager / Scheduler             │
                       └───────────────┬──────────────────────────┘
                                       ▼
                       ┌──────────────────────────────────────────┐
                       │  Orchestrator Engine                     │
                       │  (dependency graph · retries · parallel) │
                       └───────┬─────────────┬────────────────────┘
                               ▼             ▼
                     ┌──────────────┐  ┌──────────────────────────┐
                     │  Stages      │  │  Plugins                 │
                     │  build/test  │  │  github · gitlab · slack │
                     │  lint/deploy │  └──────────────────────────┘
                     └──────┬───────┘
                            ▼
              ┌─────────────────────────────┐     ┌──────────────────┐
              │  Runners                    │────▶│ Artifacts/Metrics│
              │  local · docker · kubernetes│     │ store · grafana  │
              └─────────────────────────────┘     └──────────────────┘
```

## Quickstart

```bash
git clone https://github.com/example/devopspipeline.git
cd devopspipeline
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .

devops --help
devops pipeline create demo --command "python -V"
devops pipeline run demo
```

### Example pipeline (YAML)

```yaml
name: web-service
description: Build, test and ship the web service
environment:
  IMAGE: registry.example.com/acme/web
max_parallel: 4
triggers:
  - kind: push
    branches: ["main", "release/*"]
stages:
  - type: command
    name: checkout
    commands:
      - git clone --depth 1 --branch main https://github.com/acme/web.git .
  - type: command
    name: test
    depends_on: [checkout]
    commands:
      - python -m pytest -q
  - type: command
    name: publish
    depends_on: [test]
    commands:
      - docker build -t "$IMAGE:latest" .
```

Run it with:

```bash
devops pipeline run --from-file pipeline.yaml
```

### Dashboard & API

```bash
uvicorn src.dashboard.app:create_app --factory --reload
# UI/API:      http://localhost:8000/api/v1/
# Prometheus:  http://localhost:8000/metrics
```

### Full stack (API + worker + Redis + Postgres)

```bash
docker compose up --build
```

## Documentation

| Document                                        | Contents                                   |
| ----------------------------------------------- | ------------------------------------------ |
| [Getting Started](docs/getting_started.md)       | Install, first pipeline, CLI basics        |
| [Pipelines](docs/pipelines.md)                   | YAML schema, triggers, environments        |
| [Plugins](docs/plugins.md)                       | Hook system, writing your own plugin       |
| [Deployment](docs/deployment.md)                 | Compose, Kubernetes, scaling, monitoring   |
| [API Reference](docs/api_reference.md)           | REST endpoints, Python API, CLI reference  |

## Development

```bash
make install-dev   # runtime + dev tooling
make lint          # ruff + mypy
make test          # pytest with coverage
make docker-build  # build the container image
```

Project layout:

```
src/            core packages (orchestrator, runners, stages, plugins,
                security, artifacts, metrics, dashboard)
cli/            Click-based command line interface
tests/          pytest suite
docs/           documentation
```

## Roadmap

- Distributed runners with lease-based queue (Redis backend)
- Cached stage outputs keyed by content hashes
- Approval gates with OIDC-linked identities
- Web UI on top of the dashboard API

## License

MIT — see the repository metadata for details.
