# SMART — Social Messaging Agent Runtime Toolkit

> **Beta, provided "AS IS".** SMART is sample code in beta and is not intended for production use as-is. It is distributed under the Apache License 2.0, on an "AS IS" basis, without warranties or conditions of any kind. Work with your security and legal teams to meet your organization's security, regulatory and compliance requirements before you deploy it. There is no long-term support or back-port guarantee; security fixes land on the `main` branch of the repository.

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

**Deploying needs a clone of the repository for now.** `socialagent deploy` runs the Terraform in the repository's `infra-terraform/`, which this package does not include yet; packaging it with the project is planned for 2026.11. To deploy, clone the repository and run `socialagent deploy` from inside the checkout, or pass `--tf-dir <checkout>/infra-terraform`; the getting-started guide there walks through it.

## Links

- Source, issues and documentation: <https://github.com/awslabs/social-messaging-agent-runtime-toolkit>
- Getting started: <https://github.com/awslabs/social-messaging-agent-runtime-toolkit/blob/main/docs/getting-started.md>
- Security: <https://github.com/awslabs/social-messaging-agent-runtime-toolkit/blob/main/SECURITY.md>
- Changelog: <https://github.com/awslabs/social-messaging-agent-runtime-toolkit/blob/main/CHANGELOG.md>
