# Deployment Guide

How to run DevOpsPipeline itself — from a laptop to a cluster.

## 1. Docker Compose (recommended start)

The bundled `docker-compose.yml` starts the full stack:

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f api
```

| Service    | Purpose                          | Port        |
| ---------- | -------------------------------- | ----------- |
| `api`      | Dashboard REST API               | 8000        |
| `worker`   | Scheduler/queue consumer (x2)    | —           |
| `redis`    | Queue + pub/sub backbone         | 6379        |
| `postgres` | Pipeline/run persistence         | 5432        |

Environment contract (override per environment):

```
DEVOPS_REDIS_URL=redis://redis:6379/0
DEVOPS_DATABASE_URL=postgresql://devops:devops@postgres:5432/devops
DEVOPS_WORKSPACE_ROOT=/tmp/devops-workspaces
DEVOPS_LOG_LEVEL=INFO
```

The compose file bind-mounts `/var/run/docker.sock` into `api`/`worker` so
the DockerRunner can spawn sibling ("doozer-style") containers.

## 2. Kubernetes

Apply a minimal deployment:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: devops-api
spec:
  replicas: 2
  selector:
    matchLabels: {app: devops-api}
  template:
    metadata:
      labels: {app: devops-api}
    spec:
      serviceAccountName: devops-orchestrator
      containers:
        - name: api
          image: ghcr.io/acme/devopspipeline:1.0.0
          ports: [{containerPort: 8000}]
          env:
            - name: DEVOPS_REDIS_URL
              valueFrom: {configMapKeyRef: {name: devops, key: redis-url}}
          readinessProbe:
            httpGet: {path: /api/v1/health, port: 8000}
```

For the KubernetesRunner, grant the service account RBAC to create/delete
pods in the CI namespace:

```yaml
kind: Role
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/log"]
    verbs: ["create", "get", "list", "watch", "delete"]
```

Jobs run as `restartPolicy: Never` pods with `ttlSecondsAfterFinished: 3600`
so failed builds are retained for log inspection, then GC'd by the control
plane.

## 3. Scaling guidance

| Bottleneck                | Remedy                                        |
| ------------------------- | --------------------------------------------- |
| Stage queue depth growing | Add worker replicas (`deploy.replicas`)       |
| Slow container startup    | Pre-pull base images / use a registry mirror  |
| Workspace disk pressure   | Enable retention (`RetentionManager.apply`)   |
| API latency               | Scale `api` horizontally behind an LB         |

Monitor with the built-in Prometheus metrics:

```
devops_queue_depth          # scale signal for workers
devops_active_runs          # current concurrency
histogram_quantile(0.95, sum(rate(devops_stage_duration_seconds_bucket[10m])) by (le, stage))
```

## 4. Provisioning dashboards

Generate Grafana JSON and drop it into your provisioning directory:

```python
from src.metrics.dashboards import DashboardBuilder, write_dashboard

builder = DashboardBuilder()
write_dashboard("/etc/grafana/provisioning/dashboards/web.json",
                builder.pipeline_dashboard("web-service"))
write_dashboard("overview.json", builder.overview_dashboard(["web-service"]))
```

## 5. Backup & restore

State that matters:

1. **Postgres** — pipelines, runs, audit trail. Use `pg_dump` nightly.
2. **Vault file** — encrypted at rest; back it up *with* its salt.
3. **Artifact store** — S3 handles durability; local stores need snapshots.

Rotate vault master keys periodically:

```python
vault.rotate_master_key(new_password)
# re-saves the store under the new derived key atomically-ish (save() last)
```

## 6. Upgrades

```bash
docker compose pull
docker compose up -d     # rolling restart; Postgres volume persists
```

Run `pytest` against staging runners before promoting new images; the test
suite exercises local execution paths without external services.
