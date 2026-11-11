"""OpenTelemetry Distributed Tracing setup and span decorators.

Links every operator request from root query -> sub-agent delegation -> PII redaction
-> external Shopify/Cloudflare adapter call -> final response.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode

F = TypeVar("F", bound=Callable[..., Any])


class InMemoryRingSpanExporter(SpanExporter):
    """Lightweight bounded span exporter supporting runtime diagnostics and evals."""

    def __init__(self, max_spans: int = 500) -> None:
        self._spans: list[ReadableSpan] = []
        self._max_spans = max_spans

    def export(self, spans: list[ReadableSpan]) -> SpanExportResult:
        for span in spans:
            if len(self._spans) >= self._max_spans:
                self._spans.pop(0)
            self._spans.append(span)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def get_finished_spans(self) -> list[ReadableSpan]:
        return list(self._spans)

    def clear(self) -> None:
        self._spans.clear()


_SPAN_EXPORTER = InMemoryRingSpanExporter()
_INITIALIZED = False


def init_telemetry(service_name: str = "alottacake-commerce-ops-agent") -> TracerProvider:
    """Initializes the global OpenTelemetry `TracerProvider` idempotently."""
    global _INITIALIZED
    current = trace.get_tracer_provider()
    if _INITIALIZED and isinstance(current, TracerProvider):
        return current

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "alottacake.commerce",
            "service.version": "1.0.0",
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(_SPAN_EXPORTER))
    trace.set_tracer_provider(provider)
    _INITIALIZED = True
    return provider


def get_tracer(module_name: str = "commerce_ops_agent") -> trace.Tracer:
    """Returns an OpenTelemetry tracer, ensuring the provider is initialized."""
    init_telemetry()
    return trace.get_tracer(module_name)


def get_recorded_spans() -> list[ReadableSpan]:
    """Returns finished OpenTelemetry spans captured during execution."""
    return _SPAN_EXPORTER.get_finished_spans()


def clear_recorded_spans() -> None:
    """Clears recorded spans (used between isolated test cases)."""
    _SPAN_EXPORTER.clear()


def traced_operation(span_name: str, attributes: dict[str, Any] | None = None) -> Callable[[F], F]:
    """Decorates sync or async functions with a linked OpenTelemetry child span."""

    def decorator(func: F) -> F:
        tracer = get_tracer(func.__module__)

        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with tracer.start_as_current_span(span_name) as span:
                    if attributes:
                        for k, v in attributes.items():
                            span.set_attribute(k, v)
                    try:
                        result = await func(*args, **kwargs)
                        if isinstance(result, dict) and result.get("status") == "error":
                            span.set_status(Status(StatusCode.ERROR, str(result.get("error_code", "tool_error"))))
                        else:
                            span.set_status(Status(StatusCode.OK))
                        return result
                    except Exception as exc:
                        span.record_exception(exc)
                        span.set_status(Status(StatusCode.ERROR, str(exc)))
                        raise

            return async_wrapper  # type: ignore[return-value]

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with tracer.start_as_current_span(span_name) as span:
                if attributes:
                    for k, v in attributes.items():
                        span.set_attribute(k, v)
                try:
                    result = func(*args, **kwargs)
                    if isinstance(result, dict) and result.get("status") == "error":
                        span.set_status(Status(StatusCode.ERROR, str(result.get("error_code", "tool_error"))))
                    else:
                        span.set_status(Status(StatusCode.OK))
                    return result
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    raise

        return sync_wrapper  # type: ignore[return-value]

    return decorator
