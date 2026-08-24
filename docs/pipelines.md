# Pipeline Reference

Everything that defines *what* runs, *when* it runs, and *how stages relate*.

## Document schema

```yaml
name: web-service          # required, [a-zA-Z0-9._-]
description: Build & ship  # optional
version: 1
max_parallel: 4            # concurrent stage cap (default 4)
timeout: null              # reserved for whole-run timeout
environment:               # injected into every stage's shell env
  IMAGE: registry.example.com/acme/web
triggers: [...]            # see below
stages: [...]              # see below
```

## Triggers

```yaml
triggers:
  - kind: push                # push | webhook | tag | schedule | manual
    branches: ["main", "release/*"]
    events: ["pull_request"]  # optional event-name filter (webhook kind)
  - kind: tag
    tags: ["v*"]
  - kind: schedule            # consumed by the Scheduler service
    schedule: "0 3 * * *"     # standard 5-field cron
```

Matching semantics:

- `branches` uses glob patterns (`fnmatch`); empty means "any branch".
- `tag` patterns match against the tag name with `refs/tags/` stripped.
- `manual` triggers only fire via explicit invocation (CLI/API).
- The **TriggerManager** evaluates inbound events against every registered
  pipeline and returns the set of pipelines to start.

## Stages

### Command stages (YAML)

```yaml
stages:
  - type: command
    name: unit-tests
    depends_on: [checkout]     # DAG edges
    timeout: 600               # seconds
    retry:
      attempts: 3
      delay: 10                # initial backoff, doubles each attempt
    script:
      - python -m pytest -q --cov=src
      - coverage xml
```

### Typed Python stages

Typed stages subclass `src.orchestrator.stage.BaseStage`, implement
`build_commands(ctx)` and optionally `post_process(ctx, result)`:

```python
from src.stages.build import BuildStage
from src.stages.test import TestStage

build = BuildStage(name="build", system="npm",
                   artifact_globs=("dist/*",), image_tag="web:${GIT_SHA}")
test = TestStage(name="test", framework="pytest", coverage_min=80.0,
                 depends_on=("checkout",))
```

`post_process` hooks add intelligence on top of raw command execution:

| Stage         | Extracts                                        |
| ------------- | ----------------------------------------------- |
| `Checkout`    | resolved commit SHA into `result.metadata`      |
| `Build`       | artifact globs → `result.artifacts`             |
| `Test`        | passed/failed counts, coverage %; enforces min  |
| `Lint`        | total issue count                               |
| `Deploy`      | environment, revision                           |

## Execution semantics

1. The engine validates the pipeline (duplicate names, unknown deps, cycles).
2. Kahn's algorithm produces a stable topological order.
3. Ready stages (all deps `SUCCESS`) launch concurrently up to
   `max_parallel`.
4. A failed/cancelled stage **cascade-skips** every transitive dependent.
5. Retry policies re-run only the failing stage.
6. Lifecycle events (`pipeline_start`, `stage_complete`,
   `pipeline_complete`) fan out to listeners and plugins.

## Environments & secrets

Pipeline-level `environment:` entries are merged over the process env for
every stage. Secrets should never be inlined — store them in the encrypted
vault and inject at run time:

```python
from src.security.vault import SecretVault

vault = SecretVault(password=os.environ["DEVOPS_VAULT_PASSWORD"],
                    store_path="~/.devopspipeline/vault.json")
environment = vault.inject_environment({"NPM_TOKEN": "npm_token"})
engine.run(pipeline, environment=environment)
```

## Composite groups

For sub-graphs inside a single stage slot:

```python
from src.orchestrator.stage import ParallelGroup, SequentialGroup

group = ParallelGroup.of(lint, unit_tests, name="checks")
```

ParallelGroup fans children across threads; SequentialGroup short-circuits at
the first failing child.

## Approval gates

`DeployStage` skips production deployments unless the run was approved:

```python
DeployStage(name="prod", deploy_environment="production")
# skipped unless ctx.variables["approved"] is truthy
```

Wire approvals to your identity provider using
`src.security.oidc.OIDCValidator` and record decisions in the `AuditLog`.
