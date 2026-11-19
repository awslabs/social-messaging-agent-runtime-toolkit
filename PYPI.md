# SMART — Social Messaging Agent Runtime Toolkit

> **Beta, provided "AS IS".** This is sample code, for non-production usage. You should work with your security and legal teams to meet your organizational security, regulatory and compliance requirements before deployment. SMART is in beta and is distributed under the Apache License 2.0, on an "AS IS" basis, without warranties or conditions of any kind. There is no long-term support or back-port guarantee; security fixes land on the `main` branch of the repository.

SMART is a Python SDK and Terraform stack for running an AI agent on WhatsApp on AWS. You bring the agent, running on Amazon Bedrock AgentCore Runtime; SMART handles the channel: webhook ingestion through AWS End User Messaging Social, media, message templates, conversation memory, scheduling and the infrastructure.

## Install

```bash
pip install smart-socialagent
```

The distribution is `smart-socialagent`; the command and the import package are `socialagent`:

```bash
socialagent init my-bot            # scaffold a project in ./my-bot
cd my-bot
pip install -r requirements.txt    # the agent's own dependencies
socialagent dev                    # run it locally, no deployed stack
```

The `minimal` blueprint runs locally with no AWS at all; the others call Amazon Bedrock, so they need AWS credentials and model access.

**Deploying still needs a clone of the repository.** The package ships the stack's Terraform: outside a clone, `socialagent bootstrap`, `socialagent status --refresh` and `socialagent config` run a copy the CLI writes to the project's `.socialagent/infra/` (managed by the CLI: edits there are overwritten). `socialagent deploy` and `socialagent destroy` refuse that copy for now, because the stack's Lambda layer is built from the SDK source in the repository; building it from this package is planned for a later release. To deploy, clone the release that matches `socialagent --version` (`git clone --branch v<version> https://github.com/awslabs/social-messaging-agent-runtime-toolkit`) and run `deploy` from your project directory with `--tf-dir <checkout>/infra-terraform`; the getting-started guide there walks through it.

## Links

- Source, issues and documentation: <https://github.com/awslabs/social-messaging-agent-runtime-toolkit>
- Getting started: <https://github.com/awslabs/social-messaging-agent-runtime-toolkit/blob/main/docs/getting-started.md>
- Security: <https://github.com/awslabs/social-messaging-agent-runtime-toolkit/blob/main/SECURITY.md>
- Changelog: <https://github.com/awslabs/social-messaging-agent-runtime-toolkit/blob/main/CHANGELOG.md>
