# Integration Checklist

Use this checklist before telling a user or product team that a General Augment
integration is ready.

## Human Checklist

- [ ] The app owner can sign in at https://app.generalaugment.com.
- [ ] A project exists for the app or tenant.
- [ ] Project API keys are stored only in backend/server secret storage.
- [ ] Provider/API keys are entered through the dashboard or CLI, not committed to code.
- [ ] The selected model tiers match expected cost, latency, and quality.
- [ ] `/v1/responses` works from the app backend with a stable `user` value.
- [ ] Idempotency keys are used for retryable turns.
- [ ] Tool actions are allowlisted and risky actions require approval.
- [ ] Memory scope, retention, and deletion behavior are accepted.
- [ ] External channel identity mapping is accepted if Telegram, WhatsApp, SMS, or other
  channels are enabled.
- [ ] Traces, logs, usage, and support bundles are available to debug incidents.
- [ ] Billing mechanism, included usage, provider-bill ownership, and support posture are
  explicit.
- [ ] Regulated data, DPA/BAA, residency, retention, and SLA scope are explicit.

## Agent Checklist

Run or ask the operator to run:

```bash
genaug doctor
genaug smoke --project <project-slug> --json
genaug verify --project <project-slug> --json
genaug onboarding verify --project <project-slug> --json
```

For tenant-owned provider capacity:

```bash
genaug model-providers list --project <project-slug>
genaug model-providers health <provider> --project <project-slug> --json
```

For tools:

```bash
genaug projects runtime-policy --project <project-slug> --json
genaug tools list --project <project-slug>
genaug tools discovery --project <project-slug> --json
```

For memory:

```bash
genaug memory profile --project <project-slug> --user <app-user-id>
genaug memory search --project <project-slug> --user <app-user-id> --query "<known fact>"
```

For observability:

```bash
genaug logs --project <project-slug>
genaug observability trace <trace-id> --project <project-slug> --json
genaug observability support-bundle --project <project-slug> --json
```

## Ready Output

Return `ready` only when the current app path has proof for:

- package installation or raw HTTP fallback;
- project/API key setup;
- first response;
- provider attribution when tenant-owned model capacity is in scope;
- memory/tool/channel/approval evidence for any enabled surface;
- trace or support-bundle evidence;
- known limits and regulated-data scope.

## Blocked Output

Return `blocked` with:

- the exact command or step that failed;
- status code and stable reason when available;
- request ID, trace ID, response ID, or artifact path when available;
- the missing secret, account permission, provider key, project setting, or user input;
- one next action that will unblock the run.
