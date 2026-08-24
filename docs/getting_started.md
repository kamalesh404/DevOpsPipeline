# Getting Started

Welcome to DevOpsPipeline! This guide takes you from a clean machine to your
first green pipeline in about five minutes.

## Prerequisites

- Python **3.10+** (`python --version`)
- Git
- Optional: Docker (for containerized runners), kubectl (for Kubernetes)

## 1. Install

```bash
git clone https://github.com/example/devopspipeline.git
cd devopspipeline
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
```

Verify the CLI is available:

```bash
devops --version
```

## 2. Your first pipeline

The fastest path is the built-in `create` command:

```bash
devops pipeline create hello --command "python -V" --command "echo done"
devops pipeline run hello
```

Expected output:

```
SUCCESS    main                     exit=0    0.42s
run 9f2c81a0b3de finished in 0.51s: SUCCESS
```

Runs are journaled locally; inspect them any time:

```bash
devops pipeline logs 9f2c81a0b3de
```

## 3. A realistic YAML pipeline

Create `pipeline.yaml`:

```yaml
name: demo-service
description: Checkout, test and package the demo service
max_parallel: 2
environment:
  PIP_DISABLE_PIP_VERSION_CHECK: "1"
triggers:
  - kind: push
    branches: ["main"]
stages:
  - type: command
    name: checkout
    script:
      - git clone --depth 1 https://github.com/acme/demo-service.git .

  - type: command
    name: test
    depends_on: [checkout]
    timeout: 300
    script:
      - python -m pytest -q

  - type: command
    name: package
    depends_on: [test]
    retry:
      attempts: 2
      delay: 5
    script:
      - python -m build --wheel
```

Validate, then run:

```bash
devops pipeline validate pipeline.yaml
devops pipeline run --from-file pipeline.yaml
```

Key concepts:

- **depends_on** builds a DAG; stages with satisfied dependencies run in
  parallel automatically.
- **timeout** is per-stage wall-clock seconds.
- **retry.attempts** adds exponential-backoff retries.

## 4. Built-in stage types

For richer behavior use the typed stages from `src.stages` (see
[docs/pipelines.md](pipelines.md) for the full schema):

| Stage           | Purpose                                        |
| --------------- | ---------------------------------------------- |
| `CheckoutStage` | Shallow git clone / PR head checkout           |
| `BuildStage`    | make · npm · cargo · docker builds             |
| `TestStage`     | pytest/jest/cargo + coverage thresholds        |
| `LintStage`     | ruff/eslint/clippy with issue counting         |
| `DeployStage`   | AWS/GCP/Azure/self-hosted with approval gates  |
| `NotifyStage`   | Slack/email/webhook notifications              |

## 5. The dashboard API

```bash
uvicorn src.dashboard.app:create_app --factory --reload
curl http://localhost:8000/api/v1/health
curl http://localhost:8000/metrics | head
```

Interactive OpenAPI docs live at <http://localhost:8000/docs>.

## 6. Where state lives

| Path                          | Contents                        |
| ----------------------------- | ------------------------------- |
| `~/.devopspipeline/state.json`| Registered pipelines            |
| `~/.devopspipeline/runs.jsonl`| Run journal                     |
| `~/.devopspipeline/plugins.json` | Plugin enable/disable flags  |

Next: read [docs/pipelines.md](pipelines.md) to master the pipeline schema,
or [docs/plugins.md](plugins.md) to wire in GitHub/Slack.
