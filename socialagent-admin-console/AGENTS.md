# SMART Admin Console — AI/Developer Context

## Quick Recovery
```bash
# from the repository root (the targets live in the root Makefile)
make ui-dev          # Local dev with proxy
make ui-deploy       # Full deploy pipeline
make ui-test         # Frontend vitest suite
make backend-test    # Backend pytest suite (backend/tests/)
```

## Architecture
- **Frontend**: React + Cloudscape (dark mode), Vite, TypeScript
- **Backend**: Single Python Lambda (`backend/handler.py`, a thin dispatcher) — logic split into `routes/<resource>.py` modules (one per resource) and `shared/*.py` helpers; routing via AWS Lambda Powertools `APIGatewayHttpResolver` decorators. `shared/config.py` is the home for env-var reads (`shared/auth.py` still reads its own Cognito settings). Authorization is the `@require_group` decorator from `shared/auth.py` on every route, directly under the route decorator — there is no global middleware; `backend/tests/unit/test_route_authorization.py` fails on a route that lacks it, has it in the wrong order, or changes level
- **Auth**: Cognito User Pool + Identity Pool, Amplify `<Authenticator>`
- **Infra**: Terraform modules: `frontend/` (S3+CloudFront+OAC), `auth/` (Cognito), `api/` (APIGW+Lambda+DynamoDB+S3)

## Pages (10 top-level + Stack Detail)
Top-level routes (`NAV_ITEMS` / `renderPage` in `App.tsx`): Overview, Stacks, Conversations, Schedules, State, Phone numbers, Events, Templates, Logs, Settings.

| Page | Route | Data Source |
|------|-------|-------------|
| Overview | `#/` | All stacks (cards) |
| Stacks | `#/stacks` | Registry + CloudWatch + AgentCore |
| Stack Detail | `#/stacks/:id` | Per-stack — 7 tabs (see below) |
| Conversations | `#/conversations` | AgentCore Memory (cross-stack) |
| Schedules | `#/schedules` | EventBridge Scheduler (every schedule in the region) |
| State | `#/state` | DynamoDB state tables (cross-stack) |
| Phone numbers | `#/phone-numbers` | Registry + EUM Social; bind/unbind lives only here |
| Events | `#/events` | CloudWatch Logs + trigger wizard |
| Templates | `#/templates` | EUM Social (cross-stack) |
| Logs | `#/logs` | CloudWatch FilterLogEvents |
| Settings | `#/settings` | DynamoDB settings table |

Conversations, Schedules, State and Phone numbers are also per-stack Stack Detail tabs. User management (Cognito) is handled via the Terraform `admin_emails` variable / the backend `users` routes, not a dashboard page.

## Stack Detail Tabs (7)
Status, Conversations, Schedules, State, Phone Numbers, Deploys, Errors

## Key Patterns
- `useApi` hook for consistent loading/error/refresh
- `useHashRouter` for hash-based routing
- `setAuthTokenProvider()` hands the API client a per-request ID token resolver (TokenSync component)
- `aws-exports.ts` reads `VITE_*` env vars baked at build time
- All dangerous ops have confirmation modals
- Nuclear reset runs step-by-step from frontend with progress
- Admin-only actions call `usePermission("admin")` (`src/hooks/use-groups.ts`), which reads the `cognito:groups` claim TokenSync provides, and pass `disabled`/`disabledReason` to the control. It is presentation only: `require_group` on the backend decides every request
- Bulk deletes run through `deleteEach` (`src/api/bulk-delete.ts`), which counts an item only once its call resolves, and counts an item whose `remove` resolves to `NOT_FOUND` (a state key the backend found nothing under, or a schedule whose delete answered 404) as not found, not deleted; `deleteFlash` reads "Deleted 1 key(s), 1 not found, 1 failed", an error with the backend's reason under the counts when anything failed and a warning when something was not found
- A refused list (`403`) replaces the table with the error Alert, as the Phone numbers page does: State, Conversations, Templates and Schedules must not keep the empty-table text or zero counts beside it. `useApi` keeps the last result when a later load fails, so a page that always renders the table will show stale rows or empty next to the 403
- The routes that read CloudWatch Logs (`PUT /api/logs`, `GET /api/stacks/<id>/errors`, `GET /api/stacks/<id>/events`) answer `{results, unreadable}`. `unreadable` lists each Lambda whose log group was not read in full as `{lambda, reason}`, the reason an AWS error code or `budget` (the request's calls or time ran out first); a log group that does not exist yet is no events, not unreadable. A route that read no group, with a call failed, answers `502` with the list, and log search answers `400` "Invalid filter pattern" without repeating the pattern. The client's `toLogRead` (`src/api/log-read.ts`) also takes the bare list an older backend answers, and a page that lists log lines shows `UnreadableLogsAlert` and never its "No errors" or "No events" empty state while `unreadable` is not empty or after a failed request, a `502` included
- The manual mock `src/api/__mocks__/client.ts` must cover every `api` method a component calls: a missing one makes the call throw, so a component test can pass on the error path for the wrong reason. `ApiError` lives in `src/api/errors.ts`, which is not mocked, so code that tells statuses apart (`instanceof ApiError`) sees the real class in tests, and a test rejects with `new ApiError(message, status)` to drive it

## Backend Route Pattern
Routes are Powertools `@app.get/put/post/delete` decorators in `backend/routes/<resource>.py`, registered on the shared resolver in `shared/resolver.py`; `handler.py` imports the route modules and calls `app.resolve`. Path params use Powertools `<name>` placeholders (e.g. `/api/stacks/<id>`).

## Auto-Registration
- A stack whose config sets `admin_registry_bucket` (and `admin_registry_prefix`) pushes `socialagent.yaml` to the registry via `aws_s3_object` on every deploy
- Dashboard reads stack metadata from the S3 registry (`socialagent.yaml` per stack)

## Deployed Infrastructure
Environment-specific (CloudFront distribution, API Gateway, Cognito User/Identity Pools,
the S3 registry bucket, the backend Lambda). Resolve concrete names/IDs from
`terraform output` in `socialagent-admin-console/infra-terraform/` or from the AWS console — never hardcode them here.

## Lambda Deploy (when Terraform hash doesn't trigger)
```bash
# from the repository root
cd socialagent-admin-console/infra-terraform
rm -rf modules/api/.build/package && mkdir -p modules/api/.build/package
python3 -m pip install -r ../backend/requirements.txt -t modules/api/.build/package --quiet --no-compile --python-version 3.12 --platform manylinux2014_x86_64 --implementation cp --only-binary=:all:
cp ../backend/handler.py modules/api/.build/package/
cp -r ../backend/shared modules/api/.build/package/shared
cp -r ../backend/routes modules/api/.build/package/routes
rm -f modules/api/.build/backend.zip
(cd modules/api/.build/package && zip -r ../backend.zip . -q)
aws lambda update-function-code --function-name <env>-socialagent-admin-api --zip-file fileb://modules/api/.build/backend.zip
```
> `handler.py` imports `shared/` and `routes/` at load time — all three must be
> in the package or the Lambda fails to import. `routes/` also carries the
> deny-by-default authorization decorators (`shared/auth.py`), so a partial
> copy would silently ship an unprotected or broken build.

## Known Constraints
- AgentCore Memory has no "list all sessions" API — conversations discovered via DynamoDB scan
- EUM Social lists phone numbers per WABA: `GetLinkedWhatsAppBusinessAccount` returns `account.phoneNumbers` for each WABA `ListLinkedWhatsAppBusinessAccounts` returns. A number is bound where a registry entry's `phone_number_id` names it (its ARN or its `phone-number-id-…` id)
- One-shot EventBridge schedules auto-delete after firing (only recurring visible in Schedules page)
- `handler.py` is a thin dispatcher (under 50 lines); all logic lives in `routes/<resource>.py` + `shared/*.py`. The Terraform packaging step (`modules/api/main.tf`) must copy `routes/` into the Lambda zip and hash it in the redeploy trigger — a top-level `from routes import ...` means a missing package = cold-start 502
- Backend tests live in `backend/tests/` (`make backend-test`); frontend has a vitest suite (`make ui-test`)
- `src/api/client.ts` passes every dynamic value in a request path through `segment()`, which URL-encodes it and refuses an empty, `.` or `..` segment; a new call must do the same. `test/api/client.test.ts` calls every path-building method with a hostile value, and it fails until a new `api` method is listed as path-building or constant-path
- `backend/requirements.txt` pins `boto3` and `botocore` at 1.43.33 or later, the first botocore with `FilterLogEvents` `startFromHead`, which the log search, Errors tab and Events page use to read the newest lines first. The SDK the Lambda runtime includes varies by Region and may be older, and on it the search falls back to reading window by window, which can miss the last minutes of a busy log group. Keep the floor; the SDK adds about 28 MB unzipped (16 MB zipped) to the package
- The packaging step (`null_resource.backend_deps` in `modules/api/main.tf`) and the manual redeploy above install `requirements.txt` for the Lambda, `--python-version 3.12 --platform manylinux2014_x86_64 --implementation cp --only-binary=:all:`, matching the function's runtime and the Powertools layer (`backend/tests/unit/test_package_target.py` checks all three agree). The host still needs `python3` 3.10 or later first on PATH, or pip 24.2 or later, since an older pip checks boto3's `Requires-Python` against the host's Python; `make backend-test` needs 3.10 or later
- `package.json` overrides esbuild to `^0.28.1`: the 0.28.1 floor excludes GHSA-g7r4-m6w7-qqqr (esbuild >=0.27.3 <0.28.1, which Vite 7's `^0.27.0 || ^0.28.0` admits), so keep it, and the cap is the range Vite 7 declares. A Vite upgrade that changes Vite's esbuild range must revise the override in the same change. Forcing an esbuild outside the range Vite declares is what crashed `make ui-dev` under Vite 6, and only `make ui-smoke` catches it; `make ui-build` and `make ui-test` don't run Vite's dependency optimizer
