"""Run LiteLLM with decoded-event heartbeats for Codex Responses streams.

LiteLLM's normal streaming keepalive is an SSE comment. That is sufficient for
network intermediaries, but Codex applies its idle timeout after SSE decoding,
where comments have already been discarded. This wrapper changes only
keepalive comments on ``/responses`` into a harmless ``response.in_progress``
event; every other endpoint retains LiteLLM's standard behavior.
"""

from __future__ import annotations

from contextlib import aclosing
from functools import wraps
from typing import Any


SSE_COMMENT_KEEPALIVE = ": ping\n\n"
CODEX_RESPONSES_KEEPALIVE = 'data: {"type":"response.in_progress"}\n\n'


def codex_compatible_keepalive(chunk: Any, request: Any = None) -> Any:
    """Return a decoded SSE event for Codex Responses keepalives only."""

    path = getattr(getattr(request, "url", None), "path", "")
    if (
        chunk == SSE_COMMENT_KEEPALIVE
        and isinstance(path, str)
        and path.rstrip("/").endswith("/responses")
    ):
        return CODEX_RESPONSES_KEEPALIVE
    return chunk


def install_codex_heartbeat() -> None:
    """Wrap LiteLLM's outgoing stream generator once per process."""

    from litellm.proxy import proxy_server

    original = proxy_server.async_data_generator
    if getattr(original, "_zeta_codex_heartbeat", False):
        return

    @wraps(original)
    async def wrapped_async_data_generator(
        response,
        user_api_key_dict,
        request_data,
        request=None,
    ):
        stream = original(
            response=response,
            user_api_key_dict=user_api_key_dict,
            request_data=request_data,
            request=request,
        )
        async with aclosing(stream):
            async for chunk in stream:
                yield codex_compatible_keepalive(chunk, request)

    wrapped_async_data_generator._zeta_codex_heartbeat = True
    proxy_server.async_data_generator = wrapped_async_data_generator


def main() -> Any:
    install_codex_heartbeat()
    from litellm import run_server

    return run_server()


if __name__ == "__main__":
    raise SystemExit(main())
