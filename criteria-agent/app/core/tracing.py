"""Tracing, trace ids and the Langfuse client."""

import base64
import json
import os
from functools import cache

import httpx
from openai import APIError
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Span, Status, StatusCode
from strands.telemetry import StrandsTelemetry
from strands.telemetry.config import get_otel_resource
from typesafe_sdk import TypeSafeAPIError

from app.core.config import client, settings
from app.core.errors import REFUSED_BY, Unusable

SERVICES = {TypeSafeAPIError: "Jev", APIError: "OpenAI"}
tracer = trace.get_tracer("criteria-agent")


@cache
def telemetry() -> StrandsTelemetry:
    """Set up tracing once."""
    config = settings()
    tagged = Resource({"deployment.environment": config.environment})
    provider = TracerProvider(resource=get_otel_resource().merge(tagged))
    trace.set_tracer_provider(provider)
    installed = StrandsTelemetry(tracer_provider=provider)
    if config.langfuse_public_key and config.langfuse_secret_key:
        pair = f"{config.langfuse_public_key.get_secret_value()}:{config.langfuse_secret_key.get_secret_value()}"
        installed.setup_otlp_exporter(
            endpoint=f"{config.langfuse_host}/api/public/otel/v1/traces",
            headers={
                "Authorization": f"Basic {base64.b64encode(pair.encode()).decode()}",
                "x-langfuse-ingestion-version": "4",
            },
        )
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        installed.setup_otlp_exporter()
    return installed


def dumped(value: object) -> str:
    """A value as JSON text, models dumped."""
    return json.dumps(
        value, ensure_ascii=False, default=lambda o: o.model_dump() if hasattr(o, "model_dump") else str(o)
    )


def observed(span: Span, given: object = None, returned: object = None) -> None:
    """Show a step's input and output in Langfuse."""
    if given is not None:
        span.set_attribute("langfuse.observation.input", dumped(given))
    if returned is not None:
        span.set_attribute("langfuse.observation.output", dumped(returned))


def trace_id() -> str:
    """The current trace id."""
    return format(trace.get_current_span().get_span_context().trace_id, "032x")


def langfuse() -> httpx.AsyncClient:
    """The Langfuse client."""
    config = settings()
    keys = (
        config.langfuse_public_key.get_secret_value(),
        config.langfuse_secret_key.get_secret_value(),
    )
    return client(auth=keys)


def failure(span: Span, error: Exception) -> dict:
    """A failed response."""
    span.record_exception(error)
    span.set_status(Status(StatusCode.ERROR, type(error).__name__))
    service = next((name for kind, name in SERVICES.items() if isinstance(error, kind)), None)
    reason = str(error) if isinstance(error, Unusable) else service and REFUSED_BY.format(service=service)
    return {
        "status": "failed",
        "error": type(error).__name__,
        **({"reason": reason} if reason else {}),
        "trace_id": trace_id(),
    }
