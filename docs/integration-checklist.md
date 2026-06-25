# Integration Checklist

Use this checklist before telling a user or product team that a General Augment
integration is ready.

## Human Checklist

- [ ] The app owner can sign in at https://app.generalaugment.com.
- [ ] `genaug auth login` browser consent is completed, or an admin key is available
  for an operator-owned setup path.
- [ ] A project exists for the app or tenant.
- [ ] Project API keys are stored only in backend/server secret storage.
- [ ] Provider/API keys are entered through the dashboard or CLI, not committed to code.
- [ ] Existing OpenAI Responses apps were migrated only after reviewing the
  `genaug migrate openai-responses --dry-run` diff.
- [ ] The selected model tiers match expected cost, latency, and quality.
- [ ] `/v1/responses` works from the app backend with a stable `user` value.
- [ ] Idempotency keys are used for retryable turns.
- [ ] Tool actions are allowlisted and risky actions require approval.
- [ ] Memory scope, retention, and deletion behavior are accepted.
- [ ] Traces, logs, usage, and support evidence are available to debug incidents.
- [ ] Billing mechanism, included usage, provider-bill ownership, and support posture are
  explicit.
- [ ] Regulated data, DPA/BAA, residency, retention, and SLA scope are explicit.

## Agent Checklist

Run or ask the operator to run:

```bash
genaug doctor
genaug setup --bootstrap --project <project-slug>
genaug providers setup --capability <capability> --project <project-slug> --health-check
genaug connectors setup --name <connector-name> --url '<secret-safe-mcp-url>' --health-check
genaug skills design --job-type <job-type> --project <project-slug> --apply
genaug smoke --project <project-slug> --evidence-output .genaug/smoke-evidence.json --json
genaug verify --project <project-slug> --json
genaug dashboard open --project <project-slug>
```

For tenant-owned provider capacity:

```bash
genaug providers setup --provider <provider> --project <project-slug> --api-key-env <ENV_VAR> --health-check
genaug providers smoke --provider <provider> --project <project-slug> --json
genaug providers readiness --project <project-slug> --json
```

For tools:

```bash
genaug tools list --project <project-slug>
genaug tools discovery --project <project-slug> --json
```

For memory (from backend code, using the SDK):

```python
client.memory_profile("<app-user-id>")
client.search_memory({"user_id": "<app-user-id>", "query": "<known fact>"})
```

For observability, persist redacted launch evidence from `smoke`:

```bash
genaug smoke --project <project-slug> --include-support-bundle \
  --evidence-output .genaug/smoke-evidence.json --json
```

## Ready Output

Return `ready` only when the current app path has proof for:

- package installation or raw HTTP fallback;
- project/API key setup;
- first response;
- smoke evidence artifact with dashboard observability URL;
- provider attribution when tenant-owned model capacity is in scope;
- memory/tool evidence for any enabled surface;
- trace or support-bundle evidence;
- known limits and regulated-data scope.

## Blocked Output

Return `blocked` with:

- the exact command or step that failed;
- status code and stable reason when available;
- request ID, trace ID, response ID, or artifact path when available;
- the missing secret, account permission, provider key, project setting, or user input;
- one next action that will unblock the run.
