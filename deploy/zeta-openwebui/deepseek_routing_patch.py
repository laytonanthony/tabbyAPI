"""Idempotent DeepSeek protocol routing patch for Zeta's OpenAI router."""

from __future__ import annotations


MARKER = "# BEGIN ZETA DEEPSEEK PROTOCOL ROUTING"


def transform(source: str) -> str:
    """Add provider-scoped Messages routing and honest processing errors."""
    required = (
        "headers['Accept-Encoding'] = 'identity'",
        "def _upstream_processing_error_status(response) -> int:",
        "def _get_generic_proxy_request_url(url: str, path: str) -> str:",
        "request_url = _get_generic_proxy_request_url(url, path)",
        "allowed_models = await get_filtered_models({'data': [model]}, user)",
    )
    if MARKER in source:
        missing = [item for item in required if item not in source]
        if missing:
            raise RuntimeError(f'Partial DeepSeek routing installation: {missing}')
        return source

    header_anchor = """    if config.get('headers') and isinstance(config.get('headers'), dict):
        headers = {**headers, **config.get('headers')}

    return headers, cookies
"""
    header_replacement = """    if config.get('headers') and isinstance(config.get('headers'), dict):
        headers = {**headers, **config.get('headers')}

    # BEGIN ZETA DEEPSEEK PROTOCOL ROUTING
    # Avoid Brotli because this deployment's aiohttp cannot decode it.
    if urlparse(url).hostname == 'api.deepseek.com':
        headers['Accept-Encoding'] = 'identity'
    # END ZETA DEEPSEEK PROTOCOL ROUTING

    return headers, cookies
"""
    helper_anchor = """def _get_responses_provider_indices(
"""
    helpers = """def _upstream_processing_error_status(response) -> int:
    \"\"\"Never report a local processing failure as upstream HTTP success.\"\"\"
    if response is not None and response.status >= 400:
        return response.status
    return 502


def _get_generic_proxy_request_url(url: str, path: str) -> str:
    \"\"\"Build a protocol-aware URL for the generic authenticated proxy.\"\"\"
    parsed = urlparse(url)
    if parsed.hostname == 'api.deepseek.com' and path in {
        'messages',
        'messages/count_tokens',
    }:
        origin = f'{parsed.scheme}://{parsed.netloc}'
        return f'{origin}/anthropic/v1/{path}'
    return f'{url.rstrip("/")}/{path}'


"""
    proxy_anchor = """    idx = 0
    model_id = payload.get('model') if isinstance(payload, dict) else None
    if model_id:
"""
    proxy_replacement = """    idx = 0
    model_id = payload.get('model') if isinstance(payload, dict) else None
    model = None
    if model_id:
"""
    selection_anchor = """        if model_id in models:
            idx = models[model_id]['urlIdx']
"""
    selection_replacement = """        model = models.get(model_id)
        if model is None:
            raise HTTPException(status_code=404, detail='Model not found')

        if user.role == 'user' and not BYPASS_MODEL_ACCESS_CONTROL:
            allowed_models = await get_filtered_models({'data': [model]}, user)
            if isinstance(allowed_models, dict):
                allowed_models = allowed_models.get('data', [])
            if not allowed_models:
                raise HTTPException(status_code=403, detail='Model not found')

        idx = model['urlIdx']
"""

    for old, new, label in (
        (header_anchor, header_replacement, 'DeepSeek compression header'),
        (helper_anchor, helpers + helper_anchor, 'DeepSeek routing helpers'),
        (proxy_anchor, proxy_replacement, 'generic proxy model state'),
        (selection_anchor, selection_replacement, 'generic proxy access check'),
        ("            request_url = f'{url}/{path}'\n", "            request_url = _get_generic_proxy_request_url(url, path)\n", 'protocol URL'),
    ):
        if source.count(old) != 1:
            raise RuntimeError(f'Expected one {label} anchor, found {source.count(old)}')
        source = source.replace(old, new, 1)

    source = source.replace(
        "status_code=r.status if r else 500,",
        "status_code=_upstream_processing_error_status(r),",
    )
    return source
