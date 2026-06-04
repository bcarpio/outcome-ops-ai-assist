"""shared._responses_backend — Bedrock Mantle Responses API backend.

Stubs in chunk 01. Filled in chunk 03 with SigV4-signed HTTPS calls
for both invoke() (JSON in/out) and invoke_stream() (SSE byte parser).
"""

from typing import Iterator

from .model_client_types import Request, Response, StreamEvent


def invoke(req: Request) -> Response:
    raise NotImplementedError("Responses backend non-streaming lands in chunk 03.")


def invoke_stream(req: Request) -> Iterator[StreamEvent]:
    raise NotImplementedError("Responses backend streaming lands in chunk 03.")
    yield  # make this a generator function for typing  # pragma: no cover
