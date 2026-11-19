# SMART Admin Console

> **Beta.** The console is optional and carries a lower stability bar than the SDK and CLI until it has had comparable production time. It is its own Terraform root (`infra-terraform/` in this directory); a SMART deployment without it loses nothing else. Routes are authorized deny-by-default and tested; see [docs/admin-console.md](../docs/admin-console.md) for what it covers and what it does not.

A Cloudscape React admin console for managing and monitoring SMART (Social Messaging Agent Runtime Toolkit) WhatsApp chatbot stacks on AWS.

> For a feature-by-feature walkthrough (every page, every Stack Detail tab,
> deployment, and Cognito setup), see [`docs/admin-console.md`](../docs/admin-console.md).

## Console URL

The console is served from a CloudFront distribution created by `make ui-deploy`
(the URL is a Terraform output). Access requires a Cognito login.

## Layout

### Global navigation (10 items)

| Page | What it shows |
|------|---------------|
| **Overview** | One card per registered stack — health, description, region, type, webhook events (24h), last deploy. Reclassified-template banner + recent errors. |
| **Stacks** | Filterable table of all stacks; select a row to open Stack Detail. |
| **Conversations** | Conversations across all stacks (AgentCore Memory), bulk delete. |
| **Schedules** | Every EventBridge schedule in the region, matched to a stack by target Lambda; cancel selected. |
| **State** | Per-user DynamoDB state across all stacks, delete keys. |
| **Phone numbers** | Every number on the WABAs linked in EUM Social, with the stack each is bound to; the only place to bind or unbind one. |
| **Events** | Cross-stack event log (API Gateway + EventBridge) plus a **Trigger event** wizard with payload templates. |
| **Templates** | Cross-stack WhatsApp templates; click a name for a WhatsApp-style preview. Flags Meta reclassification. |
| **Logs** | CloudWatch log search across all stack Lambdas (stack + time-window + filter pattern). |
| **Settings** | AWS profile/region, notification preferences, AWS console quick links. |

Conversations, Schedules, State and Phone numbers are also per-stack tabs inside Stack Detail.

### Stack Detail (7 tabs, Status is default)

`Status | Conversations | Schedules | State | Phone Numbers | Deploys | Errors`

- **Status** — per-Lambda health probes, invocation/error metrics, restart Lambda / runtime
- **Conversations** — paginated (25/page, "Load more"), chat modal with text/template reply, bulk delete
- **Schedules** — EventBridge schedules scoped to the stack, cancel selected
- **State** — per-user DynamoDB key-value state, delete keys
- **Phone Numbers** — EUM Social numbers bound to the stack, change SNS destination
- **Deploys** — CodeBuild deploy history
- **Errors** — recent Lambda error log entries (24h)

If a tab's API call errors or times out (10s), that tab shows an inline error
without affecting the other tabs or the global nav.

## Operations

| Operation | UX |
|-----------|-----|
| Send WhatsApp message (text or template) | Sidebar button, always accessible |
| Reply in conversation (text or template) | Reply bar in chat modal |
| Trigger event to stack | Wizard with payload templates |
| Update Lambda env vars | Confirmation modal |
| Restart Lambda (cold start) | Confirmation modal |
| Restart AgentCore Runtime | Confirmation modal |
| Change SNS destination (admin; registered topics on the same WABA only) | Confirmation modal |
| Delete conversation | Button in chat modal + confirm |
| Bulk delete conversations | Multi-select + confirm |
| Delete state keys | Multi-select + confirm |
| Cancel schedules | Multi-select + confirm |
| Nuclear reset | Type stack name + step-by-step progress |
| Bind/unbind phone numbers (admin to take a WABA from a topic no stack uses) | Modal with stack selector |

### Stack discovery

A stack registers itself on every deploy when its config sets `admin_registry_bucket` (and `admin_registry_prefix`): its Terraform writes the stack's `socialagent.yaml` metadata to the shared S3 registry bucket via `aws_s3_object`, and the console lists that bucket to discover stacks. See [`docs/admin-console.md`](../docs/admin-console.md) for the values to set.

### Security
- Cognito User Pool (admin-only creation, TOTP MFA required, 12+ char passwords)
- API Gateway JWT authorizer on all endpoints
- Deny-by-default `viewer`/`operator`/`admin` group check on every backend route, against live Cognito membership (a deleted or disabled user gets no groups)
- Error responses carry the request id, never exception text
- S3 BlockPublicAccess + OAC + HTTPS enforcement
- CloudFront security headers (HSTS, CSP, X-Frame-Options DENY)
- Conversation text is HTML-escaped before it renders, every value in a request path is URL-encoded (empty, `.` and `..` segments are refused), and a `[Template: <name>]` line is looked up only for a valid WhatsApp template name, so a WhatsApp message cannot add markup or redirect an authenticated request

## Architecture

```
CloudFront (S3 + OAC) → React SPA (Cloudscape dark mode)
                              ↓ fetch with JWT
API Gateway (Cognito JWT) → Lambda (Python, path-routed)
                              ↓
                    S3 Registry (socialagent.yaml per stack)
                    DynamoDB (admin settings + stack state tables)
                    CloudWatch (metrics, logs)
                    AgentCore Memory (conversations)
                    AgentCore Control (runtime status)
                    EUM Social (templates, phone numbers)
                    EventBridge Scheduler (schedules)
                    CodeBuild (deploy history)
                    Cognito (user management)
                    API Gateway (event API key lookup)
```

The backend is a single Python Lambda. Routes are AWS Lambda Powertools `@app.get/put/post/delete` decorators in `backend/routes/<resource>.py`, registered on the shared `APIGatewayHttpResolver` in `backend/shared/resolver.py`; `backend/handler.py` imports the route modules and calls `app.resolve`. Powertools also provides structured logging and tracing, and `backend/tests/` holds the backend suite.

## Commands

```bash
make ui-dev          # Vite dev server (proxies /api → localhost:8000)
make ui-build        # Production build (tsc && vite build)
make ui-test         # Run the vitest suite
make ui-smoke        # Start the dev server headless and check it serves the app
make backend-test    # Run the backend (pytest) suite
make ui-deploy       # Full deploy: infra → build → upload → invalidate
make ui-plan         # Terraform plan
make ui-apply        # Terraform apply
make ui-destroy      # Terraform destroy
make ui-invalidate   # CloudFront cache invalidation
```

## Local Development

`make ui-test` is the fastest verification — it runs the vitest suite fully
offline (no AWS, no auth).

To run the SPA in a browser with `make ui-dev`, create `socialagent-admin-console/.env.local`
with the Cognito + API values so login and data fetches work:

```bash
VITE_API_URL=https://<your-api-id>.execute-api.us-east-1.amazonaws.com/api
VITE_USER_POOL_ID=us-east-1_xxxxxxxxx
VITE_USER_POOL_CLIENT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxx
VITE_IDENTITY_POOL_ID=us-east-1:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
VITE_AWS_REGION=us-east-1
```

The backend Lambda reads live AWS resources (S3 registry, CloudWatch, AgentCore,
EUM Social, …) and has no offline mode — point `VITE_API_URL` at a deployed API
to see real data. Without `.env.local`, `make ui-dev` still renders the UI but
data calls and Cognito login will not resolve.

## Adding Admin Users

Edit `infra-terraform/envs/dev.tfvars`:
```hcl
admin_emails = ["user1@example.com", "user2@example.com"]
```
Then `make ui-apply`. The pool is admin-create-only, so this (or the AWS Cognito
console) is the way to add operators.

## Registering a Stack

Automatic — add to the project's `socialagent.<env>.yaml`:
```yaml
description: My bot description
terraform:
  admin_registry_bucket: <your-admin-registry-bucket>   # the console's `registry_bucket` output
  admin_registry_prefix: admin/registry
```
Then `socialagent deploy` (or `make tf-apply ENV=<project>-<env>` from the repository root, which runs it) registers the stack in the console's S3 registry. Once its `socialagent.yaml` is present, it appears on Overview and Stacks. See [How a stack gets into the console](../docs/admin-console.md#how-a-stack-gets-into-the-console).

## Project Structure

```
socialagent-admin-console/
├── src/
│   ├── App.tsx                        # Router, nav, layout, auth, send message
│   ├── types.ts                       # All TypeScript types
│   ├── api/client.ts                  # API methods
│   ├── hooks/                         # use-api, use-hash-router, use-auth
│   ├── components/                    # Modals: conversation, template, send message, payload
│   └── pages/                         # overview, stacks, stack-detail, templates, events,
│                                      #   logs, settings + reused tab pages (conversations,
│                                      #   schedules, state, phone-numbers)
├── backend/
│   ├── handler.py                     # Python Lambda — endpoint handler functions
│   └── routes/                        # Powertools route modules (one per resource)
├── test/                              # vitest tests
├── infra-terraform/modules/           # frontend/, auth/, api/
└── AGENTS.md                          # AI context recovery
```
