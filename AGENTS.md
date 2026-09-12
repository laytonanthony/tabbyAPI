# Zeta model integration requirements

These requirements apply whenever a model is added, enabled, renamed, moved
between runtimes, or exposed to any Zeta desktop, web, mobile, Codex-derived,
Claude-derived, or third-party client.

## Definition of ready

Never mark a model online, supported, or ready merely because it appears in a
provider `/models` response or an upstream request returns HTTP 200. A model is ready
only after its actual app-facing routes have passed end-to-end tests through
the public Zeta origin using an authorized credential with the same role and
permissions as the intended user.

## Required model contract

Record and publish the exact, case-sensitive served model ID and safe public
catalogue metadata. Metadata must include the display name, native and enforced
context window, supported input modalities, supported capabilities, supported
reasoning levels, default reasoning level, and availability. Use values
verified against the checkpoint, runtime, and provider documentation. Do not
copy settings from another model or advertise aliases/levels the provider only
rejects.

Availability must mean a usable inference route. Provider `/models` discovery
alone is insufficient. Keep enabled-but-offline catalogue entries visible when
intended, but report them as offline or unknown rather than online.

## Protocol routing

Resolve the exact model to its configured provider. Never allow an unresolved
model to fall back to provider index zero. Build URLs per provider and protocol:
Responses, Chat Completions, Anthropic Messages, and token-count endpoints may
use different bases and suffixes. Do not globally add `/v1`, `/responses`,
`/chat/completions`, `/messages`, or `/anthropic`.

Keep provider credentials on the Zeta server. Clients send only their Zeta
credential to the public Zeta origin. Apply authentication, model enablement,
and the existing user/group access grants before opening an upstream request.
Never log or return provider keys, user credentials, authorization headers,
cookies, private URLs, or secret configuration.

## Reasoning and capabilities

Verify reasoning end to end, including the exact request field used by each
protocol/runtime, supported effort values, default behavior, thinking on/off,
reasoning stream events, and usage accounting. Check runtime translations such
as `xhigh` mapping rather than assuming labels are equivalent. Unsupported
values must not be published.

Where advertised, verify system prompts, images/files, custom tools, tool
choice, parallel tools, tool-use/tool-result continuation, token counting,
context reporting, and automatic compaction compatibility. Hosted/server tools
must not be advertised merely because a schema accepts their names.

## Streaming and errors

For every protocol used by an app, test both non-streaming and streaming. Check
the complete protocol-native lifecycle, terminal event, response model ID,
finish reason, token usage, disconnect behavior, and malformed upstream output.
For Anthropic Messages this includes `message_start`, content block start/delta/
stop, `message_delta`, `message_stop`, and protocol-shaped errors.

Upstream failures, decoding failures, malformed responses, and local processing
exceptions must return an honest non-2xx status with a safe body. Never return
HTTP 200 containing an error payload. Do not infer success from HTTP status
alone; validate the expected content and terminal event.

## Acceptance and regression checks

Before deployment, add focused regression tests and run syntax/static checks.
After deployment, test through the public Zeta URL with an intended-user
credential:

1. Minimal non-streamed generation returns the expected text and exact model.
2. Streamed generation reaches the correct terminal event with meaningful usage.
3. Token counting works when the client requires it.
4. A harmless custom tool call and tool result complete correctly.
5. Every advertised reasoning level works; the configured default is verified.
6. Invalid/missing Zeta credentials and inaccessible models fail safely.
7. Existing working local and paid models still pass their critical route and
   default-reasoning checks.
8. Catalogue and status endpoints expose accurate metadata and availability.

Do not alter desktop interfaces, unrelated applications, working model
deployments, global provider bases, or user security policy unless the task
explicitly requires it. Back up mutable configuration, preserve rollback data,
deploy through the existing Zeta workflow, and report tested behavior separately
from code inspection. If any required check is blocked or fails, state exactly
what remains and do not call the model ready.
