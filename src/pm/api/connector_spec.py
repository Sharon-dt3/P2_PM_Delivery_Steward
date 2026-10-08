"""The OpenAPI file a Power Platform custom connector is made from.

FastAPI writes OpenAPI 3.1; the connector importer is dependable with 3.0 (and, in practice, 3.1's `anyOf: [X, {type: null}]` for an
optional field is where an import trips). This turns the API's own document into the 3.0 file, with the address Microsoft's cloud will
call, without changing the original: a pure function, tested.
"""

from __future__ import annotations

import copy

_NULL = {"type": "null"}


def _nullable(node) -> None:
    """`anyOf: [X, {type: null}]` becomes X with `nullable: true`, everywhere."""
    if isinstance(node, dict):
        union = node.get("anyOf")
        if isinstance(union, list) and _NULL in union:
            rest = [member for member in union if member != _NULL]
            del node["anyOf"]
            if len(rest) == 1:
                node.update(rest[0])
            else:
                node["anyOf"] = rest
            node["nullable"] = True
        for value in list(node.values()):
            _nullable(value)
    elif isinstance(node, list):
        for value in node:
            _nullable(value)


def _always_send(out: dict, headers: dict[str, str]) -> None:
    """Every operation gets each header as a hidden (internal) parameter with its value as the default: the connector sends it on every
    call and a flow author never sees it. Needed when the address sits behind something that treats a browser-like caller differently
    (a free tunnel's warning page), which a connector's runtime looks like."""
    for methods in out.get("paths", {}).values():
        for operation in methods.values():
            parameters = operation.setdefault("parameters", [])
            for name, value in headers.items():
                parameters.append({
                    "name": name, "in": "header", "required": True, "x-ms-visibility": "internal",  # Power Platform: internal + a default means required
                    "schema": {"type": "string", "default": value},
                })


def connector_spec(spec: dict, server_url: str, *, always_send: dict[str, str] | None = None) -> dict:
    out = copy.deepcopy(spec)
    _nullable(out)
    out["openapi"] = "3.0.3"
    out["servers"] = [{"url": server_url.rstrip("/")}]
    if always_send:
        _always_send(out, always_send)
    return out
