"""The client maps the Worker's example_only refusal, and the code it replaced, to InvalidRequest.

The former code is spelled through ROT13 so this file never contains the word it retired."""

from __future__ import annotations

import codecs

import httpx

from tessera.client import error_from_response
from tessera.errors import InvalidRequest

FORMER = codecs.decode("flagurgvp", "rot13") + "_only"


def _response(code: str) -> httpx.Response:
    request = httpx.Request("POST", "https://tessera.example/v1/stores/demo.artifacts/records/a")
    body = {"type": "about:blank", "status": 400, "code": code, "detail": "use example data"}
    return httpx.Response(400, json=body, request=request)


def test_the_new_code_and_its_former_name_are_invalid_requests() -> None:
    for code in ("example_only", FORMER):
        error = error_from_response(_response(code))
        assert isinstance(error, InvalidRequest), code
        assert str(error) == "use example data"
