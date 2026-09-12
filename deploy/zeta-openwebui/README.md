# Zeta OpenWebUI server overlay

This overlay adds an authenticated model catalogue and web-based catalogue
metadata editor to the Zeta OpenWebUI deployment. The existing OpenWebUI
model `Enabled` switch is also enforced consistently by the OpenAI-compatible
model list and execution routes, so disabling a paid provider model removes
it from Zeta and prevents outbound use. It also provides a public, read-only
feed for externally signed Zeta desktop releases.

## Architecture and source of truth

The catalogue is a safe projection of the existing OpenWebUI `model` table.
That table already owns each model ID, display metadata, active flag, owner,
and access grants. The overlay does not introduce a second catalogue file or
copy provider addresses into a public response.

## DeepSeek protocol routing

The authenticated `/openai/messages` proxy keeps its public URL stable while
routing DeepSeek models to the provider's separate Anthropic base:
`https://api.deepseek.com/anthropic/v1/messages`. OpenAI Chat Completions and
Responses continue to use the configured OpenAI base. DeepSeek calls request
identity encoding because the deployed aiohttp runtime cannot decode the
provider's Brotli responses. A failure while decoding or processing an
upstream 2xx response is returned as `502`, never as a successful HTTP 200.

The generic model-routed proxy rejects unresolved IDs instead of falling back
to provider zero and applies the same ordinary-user model access filtering as
the Responses route. Provider credentials remain server-side.

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

## Signed desktop update feed

The overlay also exposes a separate public, read-only stable update feed. It
does not use OpenWebUI authentication and has no upload, signing, or mutation
route. Desktop updaters should send no `Authorization` header:

| Request | Response |
|---|---|
| `GET /api/desktop/updates/stable/latest.json` | Exact publisher-supplied UTF-8 manifest bytes. |
| `GET /api/desktop/updates/stable/latest.json.sig` | Exact publisher-supplied canonical ASCII base64 signature text. |
| `GET /api/desktop/updates/stable/Zeta-Setup-{version}.exe` | The retained installer named by a verified manifest. |

Manifest and signature responses use `Cache-Control: no-cache`, a strong
SHA-256 `ETag`, `X-Content-Type-Options: nosniff`, and support
`If-None-Match`/`304`. Installers use `application/octet-stream`, an exact
`Content-Length`, a content-disposition filename, the manifest SHA-256 as
their strong ETag, and
`Cache-Control: public, max-age=31536000, immutable, no-transform`.
The pinned-file download response also supplies byte-range support. The update router is
registered before the root SPA mount, and its catch-all returns JSON, so an
unknown update URL can never become a `200 text/html` SPA response.

Before the first publication, both `latest.json` and `latest.json.sig` return
an explicit JSON `404`:

```json
{"detail":"No stable Zeta desktop release is published"}
```

Unknown versions and filenames return a generic JSON `404`. Invalid or
unreadable signed state fails closed with a generic JSON `503`; filesystem
paths, signature material, and exception text are not returned or logged.

### Manifest and publication contract

`latest.json` is a bounded UTF-8 JSON object. Its canonical Windows shape is:

```json
{
  "schemaVersion": 1,
  "product": "Zeta",
  "channel": "stable",
  "version": "1.2.3",
  "publishedAt": "2026-09-10T12:00:00.0000000+00:00",
  "minimumSupportedVersion": "1.0.0",
  "mandatory": false,
  "releaseNotes": "Plain-text release notes.",
  "installer": {
    "url": "https://www.zeta-ai.co.uk/api/desktop/updates/stable/Zeta-Setup-1.2.3.exe",
    "size": 12345678,
    "sha256": "lowercase-64-character-sha256"
  }
}
```

The top-level allowlist is exactly `schemaVersion`, `product`, `channel`,
`version`, `publishedAt`, `minimumSupportedVersion`, `mandatory`, optional
`releaseNotes`, and `installer`. The installer allowlist is exactly `url`,
`size`, `sha256`, and optional `authenticode`; `installer.filename` is not part
of the signed Windows schema. All top-level fields except `releaseNotes` are
required; `url`, `size`, and `sha256` are required inside `installer`.
`schemaVersion` must be the integer `1` (the legacy `schema_version` spelling
is rejected). `publishedAt` is an ISO-8601 timestamp no longer than 64
characters,
`minimumSupportedVersion` is a strict Semantic Version or `null`, `mandatory`
is a boolean, and `releaseNotes` is bounded plain text.

The release `version` uses strict Semantic Versioning. The URL is validated
before its final path component is used as the internal installer filename. It
must be an absolute, credential-free HTTPS URL with a hostname and no query,
fragment, whitespace, backslash, percent-encoded path component, empty path
component, `.` component, or `..` component. Its final component must equal
exactly `Zeta-Setup-{version}.exe`. Installer size and SHA-256 must match its
bytes. Optional `authenticode` contains only a non-empty `publisherSubject`
(at most 512 characters) and an uppercase 40- or 64-character
`certificateThumbprint`.

The detached signature is canonical base64 text (optionally ending in one LF
or CRLF) containing an RSA PKCS#1 v1.5/SHA-256 signature over the **exact
manifest bytes**. The server contains only an RSA public key of at least 2048
bits; signing and private-key custody remain outside this repository and
application.

The publisher and OpenWebUI runtime require the Python `cryptography` package;
the deployed `/home/anthony/zeta-workspace/venv` already provides it. A client
whose manifest and signature requests straddle an atomic pointer change will
fail signature verification safely and must refetch the pair before retrying.

`release-summary.json` is a bounded publisher-supplied JSON object retained
with the release for administration. If it supplies `product`, `channel`, or
`version`, each must match the manifest. It is not a public endpoint.

### External storage and local publisher

The service and publisher must use the same two settings:

| Setting | Default |
|---|---|
| `ZETA_DESKTOP_UPDATE_DIR` | `/home/anthony/.local/share/zeta/desktop-updates` |
| `ZETA_DESKTOP_UPDATE_PUBLIC_KEY` | `/home/anthony/.config/zeta/desktop-updater/manifest-public-key.pem` |

Both paths must be absolute and outside the live OpenWebUI application source
tree. Provision the key parent directory as `0700` and the public PEM as
`0600`; do not place a private key on the server. Publication creates the
release root and release directories as `0700`, with pointers, artifacts,
its dedicated-store marker, and its local lock as `0600`. A pre-created
release root must be empty, owned by the service user, and already mode
`0700`; broad or unrelated directories are refused without changing them.
The installer deliberately neither creates these directories nor publishes a
release, so `--check` is side-effect free.

Publish already-built, externally signed artifacts locally on the server:

```bash
/home/anthony/zeta-workspace/venv/bin/python \
  deploy/zeta-openwebui/publish_desktop_release.py \
  --release-dir /home/anthony/.local/share/zeta/desktop-updates \
  --public-key /home/anthony/.config/zeta/desktop-updater/manifest-public-key.pem \
  publish \
  --exe /absolute/build/Zeta-Setup-1.2.3.exe \
  --latest /absolute/build/latest.json \
  --sig /absolute/build/latest.json.sig \
  --release-summary /absolute/build/release-summary.json
```

The CLI has no network or signing capability. It verifies the signature,
contract, filename, full installer size/hash, and version ordering; stages
the installer first; fsyncs immutable release files; and atomically replaces
the small `current-version` pointer last. Normal publication cannot downgrade
or mutate an existing version. Complete older releases remain retained and
can be selected only by an explicit verified rollback:

```bash
/home/anthony/zeta-workspace/venv/bin/python \
  deploy/zeta-openwebui/publish_desktop_release.py \
  --release-dir /home/anthony/.local/share/zeta/desktop-updates \
  --public-key /home/anthony/.config/zeta/desktop-updater/manifest-public-key.pem \
  rollback 1.2.2
```

Publication and rollback take effect on the next request; they need no Zeta
desktop release, frontend build, or OpenWebUI restart. Changing service
environment variables still requires a normal service restart.

## Deployment and rollback

The current OpenWebUI runtime directory is not a Git checkout. The reviewed
code therefore lives here in the `tabbyAPI` repository and is installed as a
small, hash-guarded overlay:

```bash
/home/anthony/zeta-workspace/venv/bin/python deploy/zeta-openwebui/install.py \
  '/path/to/openwebui/backend' --check
/home/anthony/zeta-workspace/venv/bin/python deploy/zeta-openwebui/install.py \
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
- installs the public update router and verified-store helper, but creates no
  update directory, key, release, or pointer;
- is idempotent and copies no database, config, key, or environment data.

Run the updater and overlay tests before deployment:

```bash
python -m pytest -q \
  tests/test_zeta_desktop_updates.py \
  tests/test_zeta_desktop_update_route.py \
  tests/test_zeta_desktop_update_publisher.py \
  tests/test_zeta_openwebui_overlay.py
```

After installation, build the OpenWebUI frontend, run the focused tests, wait
for active requests to drain, restart only OpenWebUI, and verify
health/auth/catalogue/status plus a disabled-model rejection. To roll back,
stop OpenWebUI, restore every file from the reported backup, remove newly
created overlay files that have no backup copy, rebuild the frontend, and
restart OpenWebUI. A maintained Zeta OpenWebUI fork is the preferred long-term
replacement for this overlay packaging.
