# Plugins

Plugins extend DevOpsPipeline without touching core code. They observe the
pipeline lifecycle and can handle inbound webhooks.

## Hook surface

Every plugin subclasses `src.plugins.base.Plugin` and may override:

| Hook                   | Invoked                          | Payload              |
| ---------------------- | -------------------------------- | -------------------- |
| `on_pipeline_start`    | when a run begins                | run dict             |
| `on_stage_complete`    | after each stage                 | stage result dict    |
| `on_pipeline_complete` | once per finished run            | run dict             |
| `handle_webhook`       | on matching inbound webhook      | type, payload, headers |
| `configure`            | before first use                 | config mapping       |

All payloads are JSON-serializable dictionaries, so plugins never import
engine internals. Hook exceptions are logged and isolated — one broken plugin
cannot fail a pipeline.

## Built-in plugins

### github
- HMAC-SHA256 webhook verification (`X-Hub-Signature-256`)
- Commit statuses (`success`/`failure`/`pending`) posted on run completion
- PR parsing helpers (`GitHubClient.parse_pull_request_event`)

```python
plugin = GitHubPlugin()
plugin.configure({"token": "ghp_...", "secret": "webhook-secret",
                  "context": "ci/devopspipeline"})
```

### gitlab
- Shared-token webhook auth (`X-Gitlab-Token`)
- MR notes on completion, MR accept, pipeline triggering via REST v4

### slack
- Block Kit message builders (header/section/fields/divider/button)
- Incoming-webhook delivery with attachment colors by status
- Interactive callback acknowledgement within Slack's 3-second SLA

```python
plugin = SlackPlugin()
plugin.configure({"webhook_url": "https://hooks.slack.com/...",
                  "notify_on": {"SUCCESS", "FAILED"}})
```

### docker_registry
- Image-ref parsing (Docker Hub/GHCR/GCR/ECR, tags + digests)
- docker-CLI push/pull/login with `--password-stdin`
- ECR/GCR reference builders for deployment pipelines

## Registering & toggling

```python
from src.plugins.base import PluginManager

manager = PluginManager()
manager.register(slack_plugin)
manager.register(github_plugin)
manager.disable("github")           # keep installed, stop delivering events
manager.fire_pipeline_start(run_dict)
```

CLI equivalent:

```bash
devops plugin list --all
devops plugin enable slack
devops plugin disable github
devops plugin install devopspipeline-datadog
devops plugin info slack
```

Enable/disable state persists in `~/.devopspipeline/plugins.json`.

## Writing your own plugin

```python
# devopspipeline_jira/plugin.py
from src.plugins.base import Plugin

class JiraPlugin(Plugin):
    def __init__(self) -> None:
        super().__init__(name="jira", version="0.1.0")

    def configure(self, config):
        self.base_url = config["base_url"]
        self.token = config["token"]

    def on_pipeline_complete(self, run):
        if run.get("status") != "SUCCESS":
            return
        ticket = run.get("event", {}).get("ticket")
        if ticket:
            print(f"would transition {ticket} to DEPLOYED")

# entry point advertisement
# pyproject.toml of your distribution:
# [project.entry-points."devopspipeline.plugins"]
# jira = "devopspipeline_jira.plugin:JiraPlugin"
```

Install it anywhere in the environment; `PluginManager.discover()` picks it
up through the `devopspipeline.plugins` entry-point group.

## Webhook ingestion flow

```
SCM ──POST /api/v1/webhooks/github──▶ dashboard routes.py
                                        │ verify signature (plugin)
                                        │ normalize event
                                        ▼
                                   TriggerManager.dispatch()
                                        │
                                        ▼
                                 PipelineEngine.run()
```
