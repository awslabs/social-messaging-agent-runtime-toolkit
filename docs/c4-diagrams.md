# SMART — C4 Model Diagrams

## Level 1: System Context

```mermaid
graph TB
    user["👤 WhatsApp User<br/><i>Sends messages, voice notes,<br/>images, documents</i>"]
    admin["👤 Admin / Operator<br/><i>Manages stacks, monitors health,<br/>sends templates</i>"]

    user --> whatsapp
    whatsapp --> eum
    eum --> socialagent
    socialagent --> eum
    socialagent --> agentcore
    socialagent --> bedrock
    admin --> socialagent

    whatsapp["📱 WhatsApp<br/><i>Meta messaging platform</i>"]
    eum["☁️ AWS EUM Social<br/><i>WhatsApp Business API bridge</i>"]
    socialagent["🤖 SMART<br/><i>Social Messaging Agent Runtime Toolkit<br/>AI-powered WhatsApp bot platform</i>"]
    agentcore["☁️ Amazon Bedrock AgentCore<br/><i>Runtime hosting, memory,<br/>session management</i>"]
    bedrock["☁️ Amazon Bedrock<br/><i>Claude<br/>Foundation model inference</i>"]

    style socialagent fill:#6366F1,color:#fff,stroke:#4F46E5
    style whatsapp fill:#22C55E,color:#fff,stroke:#16A34A
    style eum fill:#F97316,color:#fff,stroke:#EA580C
    style agentcore fill:#8B5CF6,color:#fff,stroke:#7C3AED
    style bedrock fill:#8B5CF6,color:#fff,stroke:#7C3AED
    style user fill:#3B82F6,color:#fff,stroke:#2563EB
    style admin fill:#3B82F6,color:#fff,stroke:#2563EB
```

## Level 2: Container Diagram (single stack)

```mermaid
graph TB
    user["👤 WhatsApp User"] --> whatsapp["📱 WhatsApp"]
    admin["👤 Admin"] --> cf

    whatsapp --> eum["☁️ EUM Social"]
    eum --> sns

    subgraph dashboard["Admin Console"]
        cf["CloudFront + S3<br/><i>React / Cloudscape SPA</i>"]
        cf --> cognito["Cognito<br/><i>JWT Auth</i>"]
        cf --> adminapi["Admin API λ<br/><i>40+ endpoints</i>"]
        adminapi --> registry["S3 Registry<br/><i>socialagent.yaml</i>"]
    end

    subgraph inbound["Inbound Processing"]
        sns["SNS Topic"] --> webhook["Webhook λ<br/><i>Parse, filter by<br/>meta_phone_number_id,<br/>transcribe, typing, respond</i>"]
    end

    subgraph agent["Agent Layer"]
        runtime["AgentCore Runtime<br/><i>Strands / LangGraph</i>"]
        memory["AgentCore Memory<br/><i>Conversation history</i>"]
        runtime --> memory
    end

    subgraph outbound["Outbound & Scheduling"]
        notification["Notification λ<br/><i>Send templates,<br/>text, rich messages</i>"]
        scheduler["Scheduler λ<br/><i>Reminders,<br/>recurring tasks</i>"]
        event["Event λ<br/><i>External triggers</i>"]
    end

    subgraph infra["Supporting Infrastructure"]
        state["DynamoDB<br/><i>User state</i>"]
        media["S3<br/><i>Media bucket</i>"]
        eb["EventBridge<br/><i>Scheduled events</i>"]
        apigw["API Gateway<br/><i>Event endpoint<br/>x-api-key + x-webhook-secret</i>"]
    end

    webhook --> runtime
    runtime --> bedrock["☁️ Bedrock<br/><i>Claude</i>"]
    runtime --> state
    webhook --> media
    webhook --> notification

    notification --> eum
    notification --> memory

    eb --> scheduler
    scheduler --> notification
    apigw --> event
    event --> notification

    adminapi --> webhook & notification
    adminapi --> memory
    adminapi --> eum

    style dashboard fill:#1E293B,color:#fff,stroke:#334155
    style inbound fill:#1E293B,color:#fff,stroke:#334155
    style agent fill:#1E293B,color:#fff,stroke:#334155
    style outbound fill:#1E293B,color:#fff,stroke:#334155
    style infra fill:#1E293B,color:#fff,stroke:#334155

    style webhook fill:#F97316,color:#fff,stroke:#EA580C
    style notification fill:#F97316,color:#fff,stroke:#EA580C
    style scheduler fill:#F97316,color:#fff,stroke:#EA580C
    style event fill:#F97316,color:#fff,stroke:#EA580C
    style adminapi fill:#F97316,color:#fff,stroke:#EA580C

    style runtime fill:#6366F1,color:#fff,stroke:#4F46E5
    style memory fill:#6366F1,color:#fff,stroke:#4F46E5
    style bedrock fill:#8B5CF6,color:#fff,stroke:#7C3AED

    style sns fill:#EC4899,color:#fff,stroke:#DB2777
    style eb fill:#EC4899,color:#fff,stroke:#DB2777

    style state fill:#3B82F6,color:#fff,stroke:#2563EB
    style media fill:#22C55E,color:#fff,stroke:#16A34A
    style registry fill:#22C55E,color:#fff,stroke:#16A34A
    style cf fill:#8B5CF6,color:#fff,stroke:#7C3AED
    style cognito fill:#F59E0B,color:#fff,stroke:#D97706
    style apigw fill:#8B5CF6,color:#fff,stroke:#7C3AED

    style user fill:#3B82F6,color:#fff,stroke:#2563EB
    style admin fill:#3B82F6,color:#fff,stroke:#2563EB
    style eum fill:#F97316,color:#fff,stroke:#EA580C
    style whatsapp fill:#22C55E,color:#fff,stroke:#16A34A
```

## Level 2 (variant): Shared WABA → shared SNS

Two or more stacks using phone numbers under the same WhatsApp Business Account (WABA)
must share a single SNS topic because EUM Social allows only one event destination per WABA.
Each stack's webhook filters incoming SNS events by `SOCIALAGENT_META_PHONE_NUMBER_ID` and drops
messages destined for its sibling.

```mermaid
graph LR
    waba["📱 Your WABA<br/><i>2 phone numbers</i>"]
    waba -->|"phone number A"| eum
    waba -->|"phone number B"| eum

    eum["☁️ EUM Social<br/><i>1 event destination per WABA</i>"]
    eum --> sns["SNS topic<br/><i>(shared by both stacks)</i>"]

    sns --> whA["Stack A webhook λ<br/><i><b>META_PHONE = A</b></i>"]
    sns --> whB["Stack B webhook λ<br/><i><b>META_PHONE = B</b></i>"]

    whA -->|"keep if metadata.phone matches"| rtA["Runtime A<br/><i>stack-a-dev</i>"]
    whA -.->|"drop if metadata.phone differs"| drop1((( )))

    whB -->|"keep if metadata.phone matches"| rtB["Runtime B<br/><i>stack-b-dev</i>"]
    whB -.->|"drop if metadata.phone differs"| drop2((( )))

    style waba fill:#22C55E,color:#fff,stroke:#16A34A
    style eum fill:#F97316,color:#fff,stroke:#EA580C
    style sns fill:#EC4899,color:#fff,stroke:#DB2777
    style whA fill:#F97316,color:#fff,stroke:#EA580C
    style whB fill:#F97316,color:#fff,stroke:#EA580C
    style rtA fill:#6366F1,color:#fff,stroke:#4F46E5
    style rtB fill:#6366F1,color:#fff,stroke:#4F46E5
    style drop1 fill:#475569,color:#fff,stroke:#334155
    style drop2 fill:#475569,color:#fff,stroke:#334155
```

## Level 3: Scaffolding model (build-time)

SMART source is organized into distinct build-time roles. Blueprints and recipes are a scaffolding model: they are templates copied into a project by `socialagent init` and `socialagent recipe add`, never imported at runtime. The agent runtime is not copied: the `socialagent` wheel installs it as `socialagent_runtime`, and each project pins that release in its `requirements.txt`.

```mermaid
graph TB
    subgraph L1["Agent Runtime Package (installed from the socialagent wheel)"]
        agent["packages/socialagent-runtime/<br/><i>state, scheduler, notify,<br/>messages, phone</i><br/><b>Transport primitives only</b>"]
    end

    subgraph L2["Recipes (reusable tool.py + skill.md bundles)"]
        direction LR
        rr["rich-messages/<br/>tool.py + skill.md"]
        rv["voice-reply/<br/>tool.py + skill.md"]
        ri["generate-image/<br/>tool.py + skill.md"]
    end

    subgraph L3["Blueprints (opinionated starting points)"]
        direction LR
        bp1["personal-assistant/<br/><i>recipes pre-wired</i>"]
        bp2["appointment-scheduler/"]
        bp3["minimal/<br/><i>no recipes</i>"]
    end

    initcli["socialagent init &lt;name&gt;<br/><i>copies a blueprint as-is<br/>+ pins socialagent[runtime]</i>"]
    addcli["socialagent recipe add &lt;name&gt;<br/><i>copies tool.py + skill.md<br/>into the project (no overwrite)</i>"]

    bp1 -.->|"copied verbatim"| initcli
    bp2 -.->|"copied verbatim"| initcli
    bp3 -.->|"copied verbatim"| initcli
    agent -.->|"pip install from the pin"| dplY
    rr -.->|"copied on demand"| addcli
    rv -.->|"copied on demand"| addcli
    ri -.->|"copied on demand"| addcli

    initcli --> project
    addcli -.->|"adds tool.py + skill.md"| project

    project["📁 Scaffolded project<br/><i>projects/&lt;your-bot&gt;/</i>"]

    project --> dplY["Dockerfile + agent.py<br/>+ requirements.txt (the pin)<br/>+ src/tools/*.py<br/>+ src/skills/*.md"]
    dplY --> runtime["☁️ Bedrock AgentCore Runtime"]

    style L1 fill:#1E293B,color:#fff,stroke:#6366F1
    style L2 fill:#1E293B,color:#fff,stroke:#22C55E
    style L3 fill:#1E293B,color:#fff,stroke:#F97316
    style agent fill:#6366F1,color:#fff,stroke:#4F46E5
    style rr fill:#22C55E,color:#fff,stroke:#16A34A
    style rv fill:#22C55E,color:#fff,stroke:#16A34A
    style ri fill:#22C55E,color:#fff,stroke:#16A34A
    style bp1 fill:#F97316,color:#fff,stroke:#EA580C
    style bp2 fill:#F97316,color:#fff,stroke:#EA580C
    style bp3 fill:#F97316,color:#fff,stroke:#EA580C
    style initcli fill:#8B5CF6,color:#fff,stroke:#7C3AED
    style addcli fill:#8B5CF6,color:#fff,stroke:#7C3AED
    style project fill:#3B82F6,color:#fff,stroke:#2563EB
    style runtime fill:#6366F1,color:#fff,stroke:#4F46E5
    style dplY fill:#3B82F6,color:#fff,stroke:#2563EB
```

**Scaffolding roles:**

| Role | Can user modify? | Mechanism |
|---|---|---|
| Agent Runtime Package (`packages/socialagent-runtime/`) | No — it is a dependency | Installed as `socialagent_runtime` from the release `requirements.txt` pins (`socialagent init` writes the pin, `socialagent runtime update` moves it); a project an older CLI scaffolded carries a copy until `runtime update` replaces it |
| Recipes (`recipes/*`) | Yes, their own copy | Copied into the enclosing project on demand by `socialagent recipe add` (never overwrites existing files) |
| Blueprints (`blueprints/*`) | Yes, their own copy | Copied verbatim at `socialagent init` |

There is no manifest, no auto-discovery, and no runtime include/exclude filtering. A project's tools and skills are whatever files it ships.
