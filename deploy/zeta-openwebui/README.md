# Zeta desktop model catalogue

This overlay adds an authenticated model catalogue and web-based catalogue
metadata editor to the Zeta OpenWebUI deployment. The existing OpenWebUI
model `Enabled` switch is also enforced consistently by the OpenAI-compatible
model list and execution routes, so disabling a paid provider model removes
it from Zeta and prevents outbound use.

## Architecture and source of truth

The catalogue is a safe projection of the existing OpenWebUI `model` table.
That table already owns each model ID, display metadata, active flag, owner,
and access grants. The overlay does not introduce a second catalogue file or
copy provider addresses into a public response.

Membership has two paths:

1. An enabled registry row whose exact ID is currently advertised by the
   OpenAI model discovery used by `/openai/responses` appears automatically,
   even if optional catalogue metadata is absent. Normal owner/read-grant
   checks still apply.
2. An active base-model row with `meta.zeta_catalog.enabled: true` remains in
   the catalogue when it is offline and absent from live discovery.

For offline persistence, `meta.zeta_catalog.enabled: false` or a missing opt-in
excludes the row; presets are also excluded while offline. A model whose
normal OpenWebUI `is_active` value is false is always excluded, even if its
provider still advertises it or `meta.zeta_catalog.enabled` is true. An active
preset is also excluded when its registered base-model chain contains a
disabled row, preventing aliases from bypassing a disabled paid service.
Unsafe/path-like IDs are always excluded. This avoids mixing Ollama,
image/video, and local-path records into the Responses catalogue. Ordinary
users also require a registered row because
that row carries their access grants. Administrators, and installations that
explicitly bypass model access control, additionally receive safe live-only
IDs because the existing Responses route permits those IDs without a model
row; those entries use conservative metadata defaults and disappear when the
provider stops advertising them.

The opt-in is an operator contract: the row's `id` must be exactly the ID the
Responses provider advertises when online. Availability is never stored in
the catalogue. `/api/v1/models/status` remains the availability source.

### Enablement versus offline retention

The two controls intentionally mean different things:

- **Model Enabled** (`is_active`) is the global administrative switch. When
  off, the model is omitted from `/api/v1/models/catalog`, `/openai/models`,
  and `/api/v1/models/status`. Chat Completions, Responses, Responses input
  tokens, embeddings, context token count, speech payloads, and generic
  model-routed OpenAI proxy calls reject the ID with the same `404 Model not
  found` response as an unknown model. This applies to administrators and
  internal calls as well as ordinary users. The generic catch-all proxy can
  enforce the switch when its request is JSON and carries a top-level
  `model`; multipart endpoints cannot be mapped reliably by this model-row
  control.
- **Keep in catalogue while provider is offline**
  (`meta.zeta_catalog.enabled`) applies only while the normal model switch is
  on. It lets the desktop keep an enabled-but-unavailable model visible and
  grey it using the status endpoint.

Registry checks happen before opening an outbound provider session. A model
registry failure returns a generic `503` rather than failing open. Raw cached
provider discovery remains unchanged internally; filtering is applied to a
copy at the public `/openai/models` boundary.

For a provider-wide emergency or billing stop, disable the OpenAI-compatible
**connection** itself in Connections settings. That is the authoritative kill
switch for every endpoint on the provider, including model-less, batch,
multipart, and direct-connection traffic that cannot be attributed to one
OpenWebUI model row. The per-model switch fully covers the JSON DeepSeek/Zeta
chat and Responses paths described above, but is not a substitute for turning
off an entire provider connection.

The merged OpenWebUI `/api/models` picker applies a direct base-model override
only to an exact provider ID. Ollama's colonless shorthand (for example,
`local` matching `local:latest`) remains available for active presets, but is
not used to disable a different exact model. Disabled exact records are
removed by object identity, so overlapping `name` and `name:cloud` records
cannot double-remove the same provider item and crash the picker.

## Endpoint and authentication

```
GET /api/v1/models/catalog
Authorization: Bearer <Zeta JWT or user API key>
If-None-Match: W/"<previous revision>"   # optional
```

Authentication uses OpenWebUI's existing `get_verified_user` dependency.
User API keys resolve to their owning user. For ordinary users, catalogue
rows are limited to models the user owns or can read through an existing
public, direct-user, or group grant. Admin and
`BYPASS_MODEL_ACCESS_CONTROL` behaviour matches `/openai/responses`.

OpenWebUI does not currently give two API keys belonging to the same user
different model scopes; both keys therefore receive the same user catalogue.
If API-key endpoint restrictions are enabled, the existing allowed endpoint
configuration must include `/api/v1/models` (the prefix covers both
`/catalog` and `/status`).

Successful responses use:

```
Cache-Control: private, max-age=5, must-revalidate
Vary: Authorization, Cookie
ETag: W/"<revision>"
```

The server re-evaluates authentication and grants before evaluating the
validator. A matching `If-None-Match` returns `304` with an empty body. The
five-second private freshness window is short enough for catalogue changes
to become visible promptly. `generated_at` is not part of the revision.

Authentication failures use the existing JSON `401`/`403` responses.
Unexpected catalogue failures return `500` with:

```json
{"detail":"Unable to build model catalogue"}
```

Logs contain only a one-way user fingerprint, model count, revision prefix,
and event/error type. Authorization headers, keys, model records, backend
addresses, and exception text are not logged.

## Response schema (version 1)

The top-level object is:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer | Schema contract version. Currently `1`. |
| `revision` | string | SHA-256 of canonical schema version plus the sorted `models` array. Stable while semantic catalogue content is unchanged. |
| `generated_at` | string | UTC ISO-8601 time at which this representation was generated. |
| `models` | array | Every catalogue model permitted for the authenticated user, including configured offline models. |

Each model contains only these fields:

| Field | Type | Allowed values / meaning |
|---|---|---|
| `id` | string | Exact Responses model ID. 1-256 safe ASCII ID characters; absolute paths and URL schemes are rejected. |
| `display_name` | string | User-facing name, at most 200 characters. |
| `description` | string or null | Optional user-facing description, at most 2,000 characters. Unsafe internal address/credential-like text is omitted. |
| `priority` | integer | `0`-`1,000,000`. Lower values sort first. |
| `visibility` | string | `list` or `hidden`. Hidden entries remain addressable but should not be shown in an ordinary picker. |
| `context_window` | integer | Validated token count from `1,024` through `4,194,304`. Default `32,768`. |
| `default_reasoning_level` | string | One of `low`, `medium`, `high`, `xhigh`, and always present in the supported list. |
| `supported_reasoning_levels` | string array | Ordered subset of `low`, `medium`, `high`, `xhigh`. Default `['medium']`. |
| `input_modalities` | string array | Ordered subset of `text`, `image`, `audio`; `text` is always present. Default `['text']`. |
| `capabilities` | object | Exactly five strict booleans: `tools`, `parallel_tools`, `images`, `web_search`, and `reasoning`. Missing or invalid values default to `false`. |

Ordering is deterministic by `priority`, then case-insensitive
`display_name`, then `id` as a final tie-breaker.

No raw `Model`, provider, or routing object is serialized. In particular the
response never includes user IDs, grants, API keys, provider URLs, ports,
headers, environment variables, filesystem paths, prompts, `params`,
`base_model_id`, `urlIdx`, or nested provider payloads.

## Configuring metadata and adding a model

Add or update the normal OpenWebUI model record through the existing admin
model-management flow. The per-model editor includes a **Zeta Desktop
Catalogue** section for these fields, stored beneath the existing extensible
`meta.zeta_catalog` object:

```json
{
  "enabled": true,
  "display_name": "Qwen 3.8 Flash – 120B",
  "description": "Zeta's local long-context coding model",
  "priority": 10,
  "visibility": "list",
  "context_window": 262144,
  "default_reasoning_level": "medium",
  "supported_reasoning_levels": ["low", "medium", "high", "xhigh"],
  "input_modalities": ["text", "image"],
  "capabilities": {
    "tools": true,
    "parallel_tools": true,
    "images": true,
    "web_search": false,
    "reasoning": true
  }
}
```

To add a model safely:

1. Configure its provider/routing through the existing Zeta/OpenWebUI
   process; do not add backend data to this overlay.
2. Create or update its base-model row. Use the exact provider model ID,
   configure the normal owner/read grants, and leave the model **Enabled**.
3. In **Zeta Desktop Catalogue**, turn on **Keep in catalogue while provider
   is offline** and fill any known metadata. This keeps the enabled record
   visible while its provider is stopped.
4. Verify the ID appears in `/openai/models` while online and that a benign
   `/openai/responses/input_tokens` request recognizes it.
5. Verify `/api/v1/models/catalog` as the intended user's API key and join it
   with `/api/v1/models/status` by exact `id`.

Unknown or malformed optional metadata is not trusted. The endpoint uses the
conservative defaults in the schema rather than forwarding raw values. A
simple legacy context label such as `128K` can be derived; ambiguous labels
such as `262K upto 1M` fall back to `32,768` unless the numeric catalogue
field is configured.

## Offline representation

An offline configured model remains unchanged in the catalogue. Availability
is reported separately and additively in the existing status response:

```json
{
  "checked_at": 1788969600,
  "data": [
    {
      "id": "GLM-5.3-Flash-ablit-exl3-4bpw",
      "state": "offline",
      "online": false,
      "loaded": false,
      "source": "unknown"
    }
  ]
}
```

Existing live status items and fields are not changed. Catalogue models that
are absent from provider discovery are appended with the offline state above.
The desktop must join on exact ID and must not send a request to a model whose
status is offline. `online: null` retains the status endpoint's existing
meaning for a statically advertised model whose provider state is unknown.

A disabled model is not an offline model: it is omitted from both catalogue
and status responses and cannot be invoked. Re-enabling it makes it eligible
for discovery immediately; if it is configured for offline retention but the
provider is unavailable, it then appears in the catalogue with the offline
status above.

Example catalogue response:

```json
{
  "schema_version": 1,
  "revision": "32f7b6…",
  "generated_at": "2026-09-09T16:55:00Z",
  "models": [
    {
      "id": "Qwen3.8-Flash-Next-CYBERSECURITY-NVFP4",
      "display_name": "Qwen 3.8 Flash – 120B",
      "description": "Zeta's local long-context coding model",
      "priority": 10,
      "visibility": "list",
      "context_window": 262144,
      "default_reasoning_level": "medium",
      "supported_reasoning_levels": ["low", "medium", "high", "xhigh"],
      "input_modalities": ["text", "image"],
      "capabilities": {
        "tools": true,
        "parallel_tools": true,
        "images": true,
        "web_search": false,
        "reasoning": true
      }
    }
  ]
}
```

## Desktop integration assumptions

This server endpoint intentionally does not return Codex prompts or execution
policy. The desktop adapter must:

- map server `id` to the Codex `slug`;
- expand reasoning-level strings into the local format;
- merge a reviewed set of local execution-policy defaults;
- use `revision`/`ETag` for refresh and retain the last valid response only as
  a private cache;
- join status by exact ID and grey out offline/unknown models;
- never turn server text into system prompts or execution policy.

No Windows or Codex files are part of this overlay. The desktop should remove
a model when it disappears from a successfully refreshed catalogue, and use
the separate status response only to grey enabled catalogue members that are
offline or unknown.

## Deployment and rollback

The current OpenWebUI runtime directory is not a Git checkout. The reviewed
code therefore lives here in the `tabbyAPI` repository and is installed as a
small, hash-guarded overlay:

```bash
python deploy/zeta-openwebui/install.py \
  '/path/to/openwebui/backend' --check
python deploy/zeta-openwebui/install.py \
  '/path/to/openwebui/backend'
```

The installer:

- validates the reviewed `main.py`, `openai.py`, `utils/models.py`, and
  model-editor source hashes (or recognizes an existing complete
  installation);
- compiles all Python before writing;
- creates a timestamped backup beneath
  `backend/.zeta-backups/model-catalog-<UTC timestamp>`;
- atomically writes the overlay modules and marked backend/frontend
  integrations;
- is idempotent and copies no database, config, key, or environment data.

After installation, build the OpenWebUI frontend, run the focused tests, wait
for active requests to drain, restart only OpenWebUI, and verify
health/auth/catalogue/status plus a disabled-model rejection. To roll back,
stop OpenWebUI, restore every file from the reported backup, remove newly
created overlay files that have no backup copy, rebuild the frontend, and
restart OpenWebUI. A maintained Zeta OpenWebUI fork is the preferred long-term
replacement for this overlay packaging.
