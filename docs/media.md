# Media Handling

## Inbound Media

When a user sends a voice note, image, video, document, sticker or location, SMART (Social Messaging Agent Runtime Toolkit) converts it to text before passing it to your agent as `payload["prompt"]`.

### Default Behavior

| Message type | Default prompt |
|---|---|
| Audio (with `TRANSCRIPTION_BUCKET` set) | Transcribed text via Amazon Transcribe; `[User sent a voice note]` if the note has no speech |
| Audio (without bucket) | `[User sent a voice note]` |
| Image | `[User sent an image]`, or `[User sent an image: {caption}]` with a caption |
| Video | `[User sent a video]`, or `[User sent a video: {caption}]` with a caption |
| Sticker | `[User sent a sticker]`, or `[User sent a sticker: {caption}]` with a caption |
| Document | `[User sent a document: {filename}]`, falling back to the caption, then to `[User sent a document]` |
| Location | `[User shared location: lat={lat}, lon={lon}, {name}, {address}]` (name and address when present) |
| Reaction | `[User reacted {emoji} to message {wamid}]`. A new reaction runs an agent turn like any other message; a reaction the user takes back arrives with no emoji and runs none, so the bot does not answer it |
| Anything else | `[User sent a {type}]` |

Replies to the bot's own interactive messages are tagged too, so the agent can tell a tap from typed text (tool calls, and with them the question asked, are stripped from restored history):

| Reply | Prompt |
|---|---|
| Reply button, or a template's quick-reply button | `[Button reply: {title}]` |
| List row | `[List reply: {title}]`, or `[List reply: {title} — {description}]` when the row has one |
| Flow or address form | `[Form reply: {form name} — {response_json}]` |

### Audio Transcription

To enable audio transcription, set the `TRANSCRIPTION_BUCKET` environment variable to an S3 bucket name (Terraform sets it for a deployed stack). SMART will:

1. Download the audio from EUM Social
2. Upload it to `s3://{bucket}/[{prefix}/]socialagent-transcribe/{job-id}`, where `prefix` is `TRANSCRIPTION_PREFIX` when set
3. Start an Amazon Transcribe job with automatic language identification
4. Poll until complete (default timeout: 90 seconds)
5. Return the transcript text
6. Clean up the S3 object and Transcribe job (the bucket keeps a copy of the audio for a while: see [How long media stays in S3](#how-long-media-stays-in-s3))

```
TRANSCRIPTION_BUCKET=my-transcription-bucket
```

> **Latency note**: Transcription adds 5–30 seconds depending on audio length, and the webhook waits that much less for the agent so that it can still answer before its Lambda times out: a slow agent turn after a long voice note ends with the `timeout` error message, and when under 10 seconds would be left to wait the agent is not called at all. Consider sending a "processing your voice note..." message before transcription starts.

### Custom Media Handling

Override the default behavior with `@app.on_media`. The hook receives the `InboundMessage` and must return a string that becomes `payload["prompt"]` for the agent.

```python
from socialagent.types import MessageType
from socialagent.media.transcriber import transcribe_audio

@app.on_media
async def handle_media(message) -> str:
    if message.type == MessageType.AUDIO:
        # app.provider is the EUMSocialProvider instance on your SocialAgentApp
        audio_bytes = await app.provider.download_media(message.media.id)
        return await transcribe_audio(
            audio_bytes,
            message.media.mime_type,
            bucket="my-transcription-bucket",
            region="us-east-1",
        )
    if message.type == MessageType.IMAGE:
        return f"[User sent an image: {message.media.caption or 'no caption'}]"
    return f"[User sent {message.type.value}]"
```

### Giving the agent the original

By default the agent sees only text: whatever `on_media` (or the default) returns becomes `payload["prompt"]`, so a photo reaches the agent as a description written in the webhook Lambda, and a follow-up question about a detail the description left out cannot be answered. If the agent's model takes images or documents, SMART can hand it the original instead.

Turn it on in `socialagent.<env>.yaml`:

```yaml
terraform:
  enable_inbound_attachments: true
```

Three things happen on deploy: the webhook Lambda stages each inbound image or document in the stack's media bucket under `<prefix>/media/inbound/<message-id>.<format>`, the runtime role may read that prefix (and nothing else new), and a lifecycle rule on a stack-owned bucket expires those objects after one day. The payload gains a top-level `attachments` list; `prompt` is still the text fallback, built exactly as before, so an agent that ignores `attachments` behaves as it did:

```json
{
  "prompt": "[User sent an image: a receipt from a hardware store]",
  "phone": "+525555550100",
  "attachments": [
    {"kind": "image", "format": "jpeg",
     "s3": {"bucket": "<media bucket>", "key": "<prefix>/media/inbound/wamid.ABC.jpeg"},
     "filename": null, "caption": "my receipt", "size": 183204}
  ],
  "socialagent": {"version": 1, "...": "..."}
}
```

In `agent.py`, build the prompt from the payload and say which kinds your model takes:

```python
from socialagent_runtime import attachments

@app.entrypoint
async def invoke(payload, context=None):
    prompt = attachments.build_prompt(payload, accepts={"image", "document"})
    with socialagent_notify_context() as outbound:
        result = agent(prompt, invocation_state={"phone": payload.get("phone", "")})
    return SocialAgentResponse(result=str(result), outbound_messages=outbound)
```

`build_prompt` returns the plain prompt string when the payload lists no attachment of an accepted kind, and otherwise a list of Converse content blocks, the text first and one `image` or `document` block per attachment with the bytes read from S3, which a Strands `Agent` accepts as its input. `accepts` is declared here, next to the model, on purpose: it is the agent author's statement of what their model takes, and a separate setting would be a second thing to keep in step with `MODEL_ID`. Keep the webhook description in place: it is what the agent gets when an attachment is over the Converse size limit (3.75 MB for an image, 4.5 MB for a document), of an unsupported type, or unreadable, each of which is logged and skipped.

Supported kinds: `image` for JPEG, PNG, GIF and WebP; `document` for PDF, CSV, DOC, DOCX, XLS, XLSX, HTML, TXT and Markdown. Audio stays transcribed, and video is not staged. `attachments_max_bytes` on the Lambda (`ATTACHMENTS_MAX_BYTES`, default 5 MiB) caps what the webhook stages at all. The cap is enforced from the S3 listing's size before the object is read, so an oversize file never enters the Lambda's memory. On a shared media bucket the lifecycle rule is yours to add, on the same prefix.

**History.** The Strands session manager persists what the agent was called with, so the image or document bytes land in AgentCore Memory with the user turn, and would be replayed to the model on every later turn: tokens on each call, and a Memory event size limit on a large file. Restoring a session strips `image` and `document` blocks the same way it strips `reasoningContent`, keeping the text block, which is the fallback description. That filter is the session manager's, in `socialagent_runtime`: the block types stripped on restore are the set `REPLAY_UNSAFE_BLOCKS` in `packages/socialagent-runtime/_history.py`. A user turn never arrives as blocks alone — `build_prompt` puts the text first and the webhook never sends an empty prompt — so the stripped turn keeps its text and the user/assistant alternation holds.

**Staging and the versioned bucket.** Staging writes each file twice. EUM Social writes the download to `<prefix>/media/<media_id>.<ext>`, and `download_media` deletes it once it has read it; nothing deletes the staged copy under `<prefix>/media/inbound/`, which expires after a day. The bucket is versioned, so neither the delete nor the expiration removes the data: each leaves it as a noncurrent version. The inbound rule removes the staged copy's version a day later, about two days after staging, but the transit copy is outside `media/inbound/`, so the bucket-wide rule removes its version, 30 days after the read. [How long media stays in S3](#how-long-media-stays-in-s3) gives every figure, the shared-bucket case and what remains to be done.

In `socialagent dev`, an attachment can point at a local file instead of S3: `{"kind": "image", "format": "png", "path": "/tmp/pic.png"}`.

## Outbound Media

Use message builders to send rich media. The builders are available in both packages — use the right one for your context.

**From handlers (`socialagent`):**

```python
from socialagent.messages import image, document, video, location

@app.on_event("listing_available")
async def notify_listing(event: dict, ctx: EventContext) -> None:
    await ctx.send_message(event["phone"], image(event["photo_url"], caption=event["property_name"]))
    await ctx.send_message(event["phone"], location(19.4204, -99.1914, name="Example Realty"))
```

**From the agent (`socialagent_runtime`):**

```python
from socialagent_runtime import notify, messages

await notify.send_message(phone, messages.image("https://example.com/listing.jpg", caption="Downtown Loft"))
await notify.send_message(phone, messages.document("https://example.com/brochure.pdf", "brochure.pdf"))
```

The same builders (`image`, `document`, `video`, `location`, etc.) are available from `socialagent.messages` (handler side) and `socialagent_runtime.messages` (agent side).

Native WhatsApp product messages use a different path: see [Sync a Meta Commerce catalog for WhatsApp](meta-commerce-catalog.md) for a project-owned feed and catalog setup.

### Media stored in S3

WhatsApp cannot fetch S3 presigned URLs, so media in S3 is uploaded to WhatsApp and sent by `media_id`. From the agent, `notify.upload_media(bucket, key)` returns the `media_id`; a queued message whose `link` is an S3 object URL is uploaded the same way when the webhook sends it. Either way the object must be in the stack's media bucket, under `voice/`, `images/` or `media/` (beneath `MEDIA_PREFIX` on a shared bucket). Any other bucket or key, or one containing `..`, is refused and logged, and nothing is uploaded. A queued message whose upload is refused or fails still goes out with its link, which WhatsApp cannot fetch from a private bucket, so it does not count as a reply: a turn that ends with no text and queued nothing else gets the `generic` error message. The webhook cannot tell a public bucket from a private one, so it counts every S3 object link this way; serve public media by an `https` URL that is not an S3 object URL, such as a CloudFront one, which counts as sent. The agent runtime's role can write only under `voice/` and `images/`, which is where the `voice-reply` and `generate-image` recipes put their files.

## How long media stays in S3

A deployed stack's media bucket is versioned, so deleting an object there keeps its data as a noncurrent version, and so does a lifecycle expiration. SMART deletes what it only needs in transit as soon as it has read it: the copy of an inbound voice note, image or document that EUM Social writes for `download_media` (`media/<media_id>.<ext>`), and transcription uploads (`socialagent-transcribe/`). Nothing deletes the images and documents staged for the agent (`media/inbound/`, with `enable_inbound_attachments`) or the agent's `voice/` and `images/` files; only the lifecycle removes them. The per-stack bucket's lifecycle expires an object 90 days after it is written and removes a noncurrent version 30 days after it becomes noncurrent. Under `media/inbound/` its `expire-inbound-attachments` rule does both after 1 day, and S3 honours the shorter of two overlapping expirations ([conflicting lifecycle actions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/lifecycle-conflicts.html)). So, with keys under the stack's prefix on a shared bucket:

| Object | Key | Kept for about |
|---|---|---|
| EUM Social's copy of an inbound voice note, image or document, once read | `media/<media_id>.<ext>` | 30 days after the read, as a noncurrent version |
| Transcription uploads, once transcribed | `socialagent-transcribe/` | 30 days |
| Images and documents staged for the agent (`enable_inbound_attachments`) | `media/inbound/` | 2 days (1 as an object, then 1 as a noncurrent version) |
| The agent's voice notes and images | `voice/`, `images/` | 120 days (90 as an object, then 30 as a noncurrent version) |

A transit copy or transcription upload whose delete fails is kept as long as the agent's files, about 120 days. Setting `enable_inbound_attachments` back to `false` disables the inbound rule too, so files staged before then are kept up to about 120 days as well.

A staged image or document is therefore gone from `media/inbound/` after about 2 days, but its transit copy under `media/<media_id>.<ext>` stays about 30. What remains is to stage straight under `ATTACHMENTS_PREFIX` (`media/inbound/`), so that no transit copy is left, or to delete the transit copy's version after the read.

The shared runtime bucket created by `socialagent bootstrap` (`stacks/` rule) applies the same 90 and 30 days to everything under `stacks/`, and it has no inbound rule: on a shared bucket, staged attachments are kept about 120 days unless you add a rule for `<prefix>/media/inbound/`. S3 runs lifecycle actions asynchronously, so removal can trail these dates. No stack role may delete an object version (`s3:DeleteObjectVersion`), so only the lifecycle removes the data; a project that needs a shorter retention changes the lifecycle rules in `infra-terraform/main.tf` (per-stack) or `infra-terraform/bootstrap/main.tf` (shared).

A per-stack bucket deployed before it had the noncurrent rule has kept every object deleted or expired since versioning was turned on, except under `media/inbound/`, whose own rule already removed them. S3 applies a new lifecycle configuration to the objects already in the bucket ([existing and new objects](https://docs.aws.amazon.com/AmazonS3/latest/userguide/lifecycle-expire-general-considerations.html)), so the first deploy with the rule makes S3 permanently remove every version that has been noncurrent for more than 30 days: media deleted or expired more than 30 days before that deploy can no longer be restored from a version. The plan shows only an in-place update of the lifecycle configuration, so nothing in the deploy warns about this. A deployer who needs that media copies it out before the deploy: `aws s3api list-object-versions --bucket <environment>-<project>-conversation-logs-<account id>` lists the versions, the noncurrent ones with `IsLatest` false, and `aws s3api copy-object --copy-source '<bucket>/<key>?versionId=<version id>' --bucket <another bucket> --key <key>` copies one. A stack on the shared runtime bucket sees no change, because the `stacks/` rule already removes noncurrent versions after 30 days.

## Supported Audio Formats

Amazon Transcribe supports: `ogg`, `mp3`, `wav`, `mp4` (AAC/M4A), `webm`, `flac`.

WhatsApp voice notes are typically `audio/ogg; codecs=opus` — SMART maps this to `ogg` automatically.
