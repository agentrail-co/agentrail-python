# Copyright 2026 Agentrail. Licensed under the Apache License, Version 2.0.
"""A requests adapter that signs trace context only for allowed origins.

``session.mount("https://", agentrail_python.requests.adapter(agentrail))``.
Each redirect is sent through the adapter again, so every actual destination is
admitted on its own. The call's step ends when ``send`` returns: with
``stream=True`` it does not wait for the body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from requests import PreparedRequest, Response
from requests.adapters import HTTPAdapter

from ._handoff import OWNED_HEADERS
from ._outbound import begin, finish, origin

if TYPE_CHECKING:
    from ._client import Client


class AgentrailAdapter(HTTPAdapter):
    def __init__(self, client: Client, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._agentrail = client

    def send(
        self,
        request: PreparedRequest,
        stream: bool = False,
        timeout: Any = None,
        verify: bool | str = True,
        cert: Any = None,
        proxies: Any = None,
    ) -> Response:
        url = request.url or ""
        parts = urlsplit(url)
        target = request.path_url
        outgoing = begin(
            self._agentrail,
            request.method or "GET",
            origin(parts.scheme, parts.hostname or "", parts.port),
            target.split("?", 1)[0],
            target,
            url,
        )
        if outgoing.strip:
            for name in OWNED_HEADERS:
                request.headers.pop(name, None)
        request.headers.update(outgoing.headers)
        if outgoing.span is None:
            return super().send(request, stream, timeout, verify, cert, proxies)
        try:
            response = super().send(request, stream, timeout, verify, cert, proxies)
        except BaseException as error:
            finish(outgoing.span, None, error)
            raise
        finish(outgoing.span, response.status_code, None)
        return response


def adapter(client: Client, **kwargs: Any) -> AgentrailAdapter:
    """An ``HTTPAdapter`` to mount on a ``requests.Session``; ``kwargs`` go to ``HTTPAdapter``."""
    return AgentrailAdapter(client, **kwargs)
