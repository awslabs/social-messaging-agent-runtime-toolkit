---
id: ADR-0014
title: "Console extensions are separate stacks that add pages through a versioned host contract"
status: Proposed
date: 2026-11-25
deciders: SMART maintainers
supersedes: []
superseded_by: null
tags: [admin-console, extensibility, security]
---

# ADR-0014: Console extensions are separate stacks that add pages through a versioned host contract

| Status | Date | Deciders |
|---|---|---|
| Proposed | 2026-11-25 | SMART maintainers |

## Context

The admin console (`socialagent-admin-console/`) is one React and Cloudscape app and one backend Lambda behind an HTTP API with a Cognito JWT authorizer, served from S3 through CloudFront with origin access control and a CSP of `script-src 'self'`. Its pages are compiled in: `NAV_ITEMS` and `renderPage` in `src/App.tsx` list them, and the backend role holds every permission any page needs. A team that wants a page the toolkit should not ship (an operator view of its own deployment pipeline, a human-handoff queue, analytics over its own data) can fork the console and port every security fix by hand, add the page upstream so its code is public and the console's role gains permissions only that team needs, or build a second console with its own login, headers and CSP to get right.

The GenAI IDP Accelerator, another AWS open-source asset that deploys into the adopter's account, solves the same problem with its Feature Platform: an extension is its own stack that never modifies the host, its UI is a UMD bundle that registers with the host and renders in the host's React tree, its API is its own and authorises against the host's user pool, and the host lists installed extensions from a registry. SMART deploys with Terraform rather than CloudFormation, and the console's CSP and its deny-by-default `require_group` (`backend/shared/auth.py`) must not loosen for an extension.

## Decision

The console becomes a host for **extensions**: each is its own Terraform root, deployed into the console's account, that adds top-level pages without changing the console's code, Terraform or roles. Version 1 of the host contract:

- **Bundle.** The extension writes its UI to `extensions/<id>/<version>/extension.js` in the console's site bucket, so the console's own distribution serves it and `script-src 'self'` holds. `<id>` matches `^[a-z][a-z0-9-]{1,30}$`, and the host routes an extension's page under `#/ext/<id>`, so no id can shadow a console page such as `#/stacks`. The bundle calls `window.SmartConsole.register(id, Component)` and nothing more: the display name and version come from the manifest alone. It externalises React, ReactDOM and Cloudscape, which the host exposes on `window`, so the page renders with the host's styles. Amplify is not on `window`.
- **Manifest.** Beside the bundle, `extensions/<id>/manifest.json` gives `id`, `displayName`, `version`, `contractVersion`, `bundle`, `apiBase` and `groups`. A new viewer route, `GET /extensions`, lists the manifests; the host builds the navigation from them, loads a bundle only from under its own `extensions/<id>/`, and refuses a manifest whose `contractVersion` it does not support. Manifests live in the site bucket, not the stack registry, because every stack deploy writes the registry.
- **Context.** The host renders the component with an `ExtensionContext`: `getAuthToken()` (the per-request Cognito ID token the console's own client uses, and the extension's only interface to the session), `apiBase`, the user's email and groups, and `SafeMarkdown`, a sanitising markdown component the host adds for untrusted text.
- **API.** Optional: the extension's own HTTP API and Lambda, with a JWT authorizer on the console's user pool and client, and its own Cognito groups in that pool, enforced deny-by-default in its backend. Because the authorizer names the console's client, the API accepts any console user's token, so the extension's groups are the only gate. Extension groups grant nothing in the console, and an extension's users must also be console viewers, since `GET /extensions` and the console's routes require at least `viewer` (`has_permission` in `backend/shared/auth.py` ranks an unknown group below it). The console's existing `connect-src` (`https://*.amazonaws.com`) admits the API, so the CSP does not change. `groups` in the manifest only hides the page; the API is the gate.
- **Typed contract.** The host publishes the contract as a TypeScript declaration (`ExtensionContext`, the `register` signature, the globals on `window`) in the console's source. Contract version 1 provides React 18 and Cloudscape 3: a host that moves either to a new major is contract version 2. An extension builds against that file, and the host's tests check that every field the declaration lists is the one the host passes, so neither side can guess at a field the other does not have.
- **Fail-closed authorization.** An extension's API refuses a caller in none of the groups it names, and an empty or missing list authorizes nobody. This is part of the contract, not the extension's choice: on a shared user pool every console user is authenticated to the extension's API, so authentication says nothing about who may read it.
- **Host values.** The extension's Terraform takes the console's existing outputs as variables: `s3_bucket_name`, `cloudfront_url` (the origin an extension API's CORS allows), `cognito_user_pool_id` and `cognito_client_id`.

Question for the maintainers: should the console host extensions on this contract, with no extension able to change the console's code, Terraform or roles?

## Alternatives considered

- **Fork the console.** Full control. Why not: every security fix is ported by hand, forever.
- **Add the pages upstream.** One console. Why not: code only one team needs becomes public and the console's role widens for every adopter.
- **A separate app per extension.** Isolated by construction. Why not: a second login, distribution and set of headers to review, and users move between consoles.
- **A sandboxed iframe per extension, talking to the host over `postMessage`.** Real isolation. Why not in version 1: the console sends `frame-ancestors 'none'` and `X-Frame-Options: DENY`, which would have to loosen, each extension ships its own React and Cloudscape, and the token crosses a message channel. An extension the account's own administrator installs is trusted like the console's own code; revisit if extensions from third parties appear.
- **Build-time inclusion** (Module Federation, or extension packages compiled into the console). Why not: every extension change rebuilds and redeploys the console, the coupling this record removes.

## Consequences

- This is not the runtime discovery [ADR-0005](0005-capabilities-are-recipes.md) rejects. That record is about agent capabilities in a project, where the framework is the adopter's choice and a tool the loader found but nobody wired is invisible. Here the host is one app the toolkit owns, an extension is a deployed stack with its own lifecycle and IAM, and the manifest is the wiring: a page appears when the extension's own `terraform apply` writes it and goes with its `destroy`. ADR-0005 stands for agent capabilities.
- A bundle runs in the console's page with each user's session, so it can do whatever that user can do in the console. The site bucket's policy limits who reads it (CloudFront only), not who writes it: the boundary is any principal of the account allowed `s3:PutObject` on the bucket, which can already replace the console's `index.html`. Publishing an extension is therefore a console-administrator power, held by whoever administers the account. The threat model gets a row for it with the implementation, and the implementation gets a security review, because it adds a way to load code into the console.
- The console uploads its build as Terraform objects (`aws_s3_object.site_files`), so a console deploy leaves `extensions/` alone. A deploy changed to `aws s3 sync --delete` would remove every extension; the implementation adds a test against that.
- CloudFront answers a 403 or 404 with `index.html` and a 200 (hash routing), so a missing bundle comes back as HTML. The host treats a bundle that has not called `register` within a timeout as failed and shows an error in its place, not a blank page.
- Bundle paths carry the version and are never rewritten, so a cached bundle is never stale; the backend reads manifests from S3 directly, not through CloudFront.
- Updating an extension writes the new bundle under its new version and then rewrites the manifest in place. Nothing in an update deletes the manifest, so the page never leaves the navigation while a new version deploys; only the extension's `terraform destroy` removes it.
- The shared libraries couple the two sides: the host exposes one copy of React and Cloudscape, and an extension that bundles its own (including `react/jsx-runtime`, which the automatic JSX transform pulls in) renders against a second React and breaks. Extensions build with the classic JSX runtime or alias `react/jsx-runtime` to the host's React. Keeping Amplify off `window` keeps an extension from reading or changing the session other than through `getAuthToken()`.
- An additive change keeps `contractVersion` 1. Removing or changing the meaning of anything in the contract is version 2, and the host keeps supporting version 1 for at least one release.
- Planned, not shipped: all of it. The host side is `GET /extensions`, `window.SmartConsole` with the shared libraries, the contract's TypeScript declaration and its test, `SafeMarkdown`, a Settings panel listing installed and refused extensions, one example extension, and a guide for extension authors.

## Open questions

Things this record does not settle, or that review may want to challenge. A question that gets answered moves into Decision or Consequences and is removed here.

- The console's `connect-src https://*.amazonaws.com`, which predates this record, admits any account's endpoint. Should the host narrow it to its own API and the `apiBase` of each installed extension, at the cost of a console apply after every install?
- Host values as Terraform variables, or published under an SSM Parameter Store path the extension reads, which saves copying them and adds parameters the console creates?
- Version 1 adds top-level pages only. Should a later version add slots inside existing pages, such as a tab on Stack Detail?

## Enforced by

Nothing yet.
