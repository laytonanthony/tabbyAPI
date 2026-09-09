# Zeta SearXNG overlay

This non-secret overlay records the search-engine mix used by the Zeta Codex
integration. It uses SearXNG's supported `use_default_settings` merge behavior,
so engine definitions continue to come from the installed SearXNG version.

The service must supply a private `SEARXNG_SECRET` environment variable and set:

```text
SEARXNG_SETTINGS_PATH=/path/to/tabbyAPI/deploy/zeta-searxng/settings.yml
```

Keep the listener on `127.0.0.1`. Remote clients must use Zeta's authenticated
HTTPS MCP endpoint; do not expose port 8888 directly.

Before switching a service to this overlay:

1. validate the YAML and set a private `SEARXNG_SECRET`;
2. run one engine-specific Yahoo query;
3. restart only SearXNG and wait for its HTTP health check; and
4. run an authenticated MCP `web_search` smoke test.

For Spanish shopping searches, fewer relevant results are preferable to ten
generic pages. Search snippets are leads—not proof of a live price, stock, VAT,
shipping, or exact GPU edition.
