"""Minimal live inference metrics for Zeta's local hardware monitor."""

from __future__ import annotations

import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


_lock = threading.Lock()
_requests: dict[str, dict[str, Any]] = {}
_generation_tokens_total = 0
_prompt_tokens_total = 0
_model_name = ""
_metrics_server: ThreadingHTTPServer | None = None


def request_started(request_id: str, model_name: str) -> None:
    """Register one active generation choice."""
    global _model_name
    now = time.monotonic()
    with _lock:
        _model_name = model_name
        _requests[request_id] = {
            "started_at": now,
            "first_token_at": None,
            "prompt_tokens": 0,
            "generated_tokens": 0,
            "tokens_per_second": 0.0,
        }


def observe_generation(request_id: str, generation: dict) -> None:
    """Update counters from one ExLlamaV3 generation result."""
    global _generation_tokens_total, _prompt_tokens_total
    now = time.monotonic()

    with _lock:
        state = _requests.get(request_id)
        if state is None:
            return

        prompt_tokens = int(generation.get("prompt_tokens") or 0)
        if prompt_tokens > 0 and state["prompt_tokens"] == 0:
            state["prompt_tokens"] = prompt_tokens
            _prompt_tokens_total += prompt_tokens

        token_ids = generation.get("token_ids") or []
        streamed_count = len(token_ids)
        reported_count = int(
            generation.get("generated_tokens")
            or generation.get("gen_tokens")
            or 0
        )
        next_count = max(state["generated_tokens"] + streamed_count, reported_count)
        delta = max(0, next_count - state["generated_tokens"])
        if delta:
            state["generated_tokens"] = next_count
            _generation_tokens_total += delta
            if state["first_token_at"] is None:
                state["first_token_at"] = now

        reported_tps = generation.get("gen_tokens_per_sec")
        if isinstance(reported_tps, (int, float)) and math.isfinite(reported_tps):
            state["tokens_per_second"] = max(0.0, float(reported_tps))
        elif state["first_token_at"] is not None:
            elapsed = max(now - state["first_token_at"], 0.001)
            state["tokens_per_second"] = state["generated_tokens"] / elapsed


def request_finished(request_id: str) -> None:
    """Remove a completed or cancelled generation from the active set."""
    with _lock:
        _requests.pop(request_id, None)


def prometheus_text() -> str:
    """Return the subset of Prometheus-style gauges consumed by Zeta."""
    with _lock:
        active_requests = len(_requests)
        live_context = sum(
            state["prompt_tokens"] + state["generated_tokens"]
            for state in _requests.values()
        )
        live_tps = sum(state["tokens_per_second"] for state in _requests.values())
        model_name = _model_name.replace("\\", "\\\\").replace('"', '\\"')
        lines = [
            "# TYPE tabbyapi:generation_tokens_total counter",
            f"tabbyapi:generation_tokens_total {_generation_tokens_total}",
            "# TYPE tabbyapi:prompt_tokens_total counter",
            f"tabbyapi:prompt_tokens_total {_prompt_tokens_total}",
            "# TYPE tabbyapi:num_requests_running gauge",
            f"tabbyapi:num_requests_running {active_requests}",
            "# TYPE tabbyapi:num_requests_waiting gauge",
            "tabbyapi:num_requests_waiting 0",
            "# TYPE tabbyapi:gen_throughput gauge",
            f"tabbyapi:gen_throughput {live_tps:.6f}",
            "# TYPE tabbyapi:num_used_tokens gauge",
            f"tabbyapi:num_used_tokens {live_context}",
        ]
        if model_name:
            lines.extend(
                [
                    "# TYPE tabbyapi:server_info gauge",
                    f'tabbyapi:server_info{{model_name="{model_name}"}} 1',
                ]
            )
        return "\n".join(lines) + "\n"


class _MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = prometheus_text().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return


def start_server(host: str = "127.0.0.1", port: int = 8003) -> None:
    """Start a dedicated metrics thread that remains responsive during GPU work."""
    global _metrics_server
    if _metrics_server is not None:
        return
    server = ThreadingHTTPServer((host, port), _MetricsHandler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever,
        name="zeta-tabby-metrics",
        daemon=True,
    )
    thread.start()
    _metrics_server = server
