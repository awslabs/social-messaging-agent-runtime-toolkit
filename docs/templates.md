# Templates

- [Defining Templates](#defining-templates)
- [Syncing Templates](#syncing-templates)
  - [Validating locally](#validating-locally)
- [Sending Templates](#sending-templates)
- [Template Prefix](#template-prefix)

WhatsApp requires pre-approved message templates for outbound messages to users who haven't messaged you in the last 24 hours. SMART (Social Messaging Agent Runtime Toolkit) provides a YAML-based template registry and a sync command to register templates with Meta via EUM Social.

## Defining Templates

Edit `templates.yaml` in your project:

```yaml
templates:
  - name: appointment_confirmed
    category: UTILITY
    language: es_MX
    components:
      - type: BODY
        text: "✅ Tu cita para ver el {{1}} ha sido confirmada para el {{2}} a las {{3}}."
        variables:
          - position: 1
            name: car
          - position: 2
            name: date
            format: date
          - position: 3
            name: time
            format: time

  - name: payment_receipt
    category: UTILITY
    language: es_MX
    components:
      - type: BODY
        text: "Tu pago de {{1}} fue procesado exitosamente."
        variables:
          - position: 1
            name: amount
            format: currency
```

### Variable Formats

| Format | Example input | Rendered output |
|--------|--------------|-----------------|
| `date` | `"2026-02-10"` | `"10 de febrero de 2026"` |
| `time` | `"09:00"` | `"09:00"` |
| `currency` | `"500"` | `"$500.00"` |
| *(none)* | `"Downtown Loft"` | `"Downtown Loft"` |

## Syncing Templates

`socialagent templates sync` registers the templates in the project's templates file with Meta via EUM Social. It runs in two places: automatically, in CodeBuild, during every deploy (`infra-terraform/modules/templates`), and by hand when you want to push template changes without a full deploy. It exits non-zero if any template is rejected, so a rejection fails the deploy.

Inside a project, everything it needs comes from `socialagent.<env>.yaml`:

```bash
uv run socialagent templates sync            # uses socialagent.dev.yaml
uv run socialagent templates sync --env prod
```

Each value is taken from the first source that has it: the flag, then `socialagent.<env>.yaml`, then an environment variable, then the default. Outside a project (in CI, for example) pass flags or set the environment variables.

| Flag | From `socialagent.<env>.yaml` | Env var | Default |
|------|-------------------------------|---------|---------|
| `--env` | — | — | `dev` |
| `--phone-number-id` | `whatsapp.phone_number_id` | `PHONE_NUMBER_ID` | none (required) |
| `--templates` | `whatsapp.templates` | `TEMPLATES_PATH` | `templates.yaml` in the project |
| `--prefix` | `whatsapp.template_prefix`, else `<environment>_<project>_` with hyphens as underscores | `TEMPLATE_PREFIX` | empty |
| `--region` | `region` | `AWS_REGION` | `us-east-1` |

The derived prefix is the one Terraform gives the deployed stack, so a manual sync registers the names the Lambdas look up.

Output:
```
Using socialagent.dev.yaml: prefix 'dev_mybot_', region us-east-1
Created: 2  Updated: 0  Deleted: 0  Skipped: 0  Failed: 0
  + dev_mybot_appointment_confirmed
  + dev_mybot_payment_receipt
```

Options:
- `--force` — re-register templates that already exist (see below)
- `--delete-removed` — delete templates registered with Meta under this prefix that are no longer in the YAML; refused when the prefix is empty. It lists what it would delete and asks first (`--yes` skips the question; without a terminal it is required). Names are matched by prefix, so another project whose prefix starts with this one (`dev_bot_` matches `dev_bot_v2_*`) is included: check the list
- `--skip-validation` — sync even when local validation finds errors (see [Validating locally](#validating-locally))

### How sync reconciles templates

Sync compares the names in your YAML against the templates already registered with Meta:

- **New** templates (in YAML, not registered) are created.
- **Already-registered** templates are **skipped** by default and reported under `Skipped`.
- **Removed** templates (registered, not in YAML) are deleted only with `--delete-removed`.

Sync does **not** diff template bodies. The provider listing API
(`list_whatsapp_message_templates`) returns only metadata — name, status,
language, category — never the body text, so a body-level diff is not possible
from the listing. Re-registering unchanged templates would also re-trigger
Meta's review, which can reclassify a UTILITY template as MARKETING.

To push a changed body, edit the YAML and run with `--force`, which
re-registers every local template regardless of current state (reported under
`Updated`):

```bash
uv run socialagent templates sync --force
```

### Body constraints Meta enforces

A body that reads fine locally can still be rejected at registration:

- **A variable cannot open or close the body.** Put static text at both ends.
- **Variable count is judged against body length.** Too many placeholders for too little surrounding text fails with `InvalidParametersException: This template has too many variables for its length.` The threshold is not a fixed variable count and is not published; bodies with six variables and ~216 characters of static text register fine. Give each variable surrounding text.
- **Emojis, branding or promotional phrasing** get the template reclassified as MARKETING, which changes the sending rules. Keep it plainly transactional.

`templates sync` exits non-zero when any template is rejected, so a bad body fails the deploy that triggers the sync.

### Validating locally

`socialagent templates validate [--env] [--templates] [--prefix]` checks `templates.yaml` for the causes above without calling AWS. A project with no templates file and no templates path configured (`--templates`, `TEMPLATES_PATH` or `whatsapp.templates`), such as one from the `minimal` blueprint, has nothing to check: the command says so and exits 0. A configured path that does not exist is still an error. `templates sync` runs the same checks on the templates it is about to send (new ones, plus every one with `--force`) and registers nothing while any of them has an **error** (`--skip-validation` overrides, for a rule you know Meta has relaxed). A template that is already registered is skipped rather than sent, so it is not checked again: one Meta has already approved never blocks a sync, including the one CodeBuild runs during a deploy. A new malformed body there fails in seconds with the rule named.

| Severity | Check |
|---|---|
| error | body opens or closes on a variable |
| error | name already carries the prefix (sync would register `dev_bot_dev_bot_x`), or is not lowercase letters, digits and underscores |
| error | placeholders not `{{1}}..{{n}}` in order; `variables` or `example.body_text` not matching them |
| error | missing or unknown `category`, missing `language`, no or several BODY components, body over 1024 characters, duplicate names |
| warning | fewer than 25 static characters per variable (a heuristic: Meta's real threshold is unknown) |
| warning | emoji, or the stack's `brand.name`, in a UTILITY template (reclassification risk) |

Warnings never block: the density threshold is a heuristic with an unknown true value, and reclassification happens after registration. Passing validation is therefore not a promise of UTILITY approval.

> **Registration is not approval.** Every template the blueprints ship registers cleanly. Meta's approval review runs afterwards and asynchronously: `templateStatus` starts as `PENDING`, and a template that registered cleanly can still come back reclassified as MARKETING, which changes the sending rules. Read the current state with `aws socialmessaging list-whatsapp-message-templates --id <waba-id>` rather than assuming a successful sync means an approved template.

## Sending Templates

### From the Notification Handler

The notification Lambda accepts `send_template` operations:

```python
# Invoked by socialagent_runtime.notify or EventContext
{
    "source": "socialagent",
    "operation": "send_template",
    "data": {
        "phone": "+5215500000000",
        "template_name": "appointment_confirmed",
        "params": ["Downtown Loft", "2026-02-10", "09:00"]
    }
}
```

### From an Event Handler

```python
@app.on_event("appointment_booked")
async def handle_booking(event: dict, ctx: EventContext):
    await ctx.send_template(
        event["phone"],
        "appointment_confirmed",
        params=[event["property"], event["date"], event["time"]],
    )
```

### From the Agent

```python
from socialagent_runtime import notify

await notify.send_template(
    phone,
    "appointment_confirmed",
    params=["Downtown Loft", "2026-02-10", "09:00"],
)
```

## Template Prefix

The `template_prefix` in `SocialAgentConfig` is applied automatically when sending templates via the notification handler. You don't need to include the prefix in your code — just use the base name:

```python
config = SocialAgentConfig(
    ...,
    template_prefix="prod_mybot_",  # applied automatically
)

# In your code, just use the base name:
await ctx.send_template(phone, "appointment_confirmed", params=[...])
# Sends: prod_mybot_appointment_confirmed
```
