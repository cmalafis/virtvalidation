#!/usr/bin/env python3
"""Generate docs/API.md from the FastAPI OpenAPI spec.

Run from the repo root:

    cd backend && python ../scripts/generate_api_docs.py

The script imports `app.main:app`, calls `app.openapi()`, and emits a
human-friendly markdown reference grouped by tag. Re-run after any route
change so docs/API.md stays in sync.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make sure the test/dev DB env is set BEFORE importing the app, exactly
# like conftest.py — otherwise pydantic-settings will fail on a missing
# DATABASE_URL when run outside the container.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("OLLAMA_HOST", "http://localhost:0")
os.environ.setdefault("OLLAMA_MODEL", "doc-gen")

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.main import app  # noqa: E402

OUT_PATH = REPO_ROOT / "docs" / "API.md"


def _resolve_ref(ref: str, components: dict) -> dict:
    # "#/components/schemas/Foo" → components["schemas"]["Foo"]
    parts = ref.lstrip("#/").split("/")
    node: dict = {"components": components}
    for p in parts:
        node = node[p]
    return node


def _format_schema_name(schema: dict, components: dict) -> str:
    if not schema:
        return "—"
    if "$ref" in schema:
        return schema["$ref"].split("/")[-1]
    if schema.get("type") == "array":
        return f"list[{_format_schema_name(schema.get('items', {}), components)}]"
    if "anyOf" in schema:
        names = [_format_schema_name(s, components) for s in schema["anyOf"]]
        return " | ".join(names)
    if schema.get("type"):
        return schema["type"]
    return "object"


def _params_table(params: list[dict]) -> str:
    if not params:
        return ""
    rows = ["| Name | In | Required | Type | Description |", "|------|----|----------|------|-------------|"]
    for p in params:
        schema = p.get("schema", {}) or {}
        type_str = schema.get("type", "object")
        if "anyOf" in schema:
            type_str = " \\| ".join(s.get("type", "object") for s in schema["anyOf"])
        if schema.get("enum"):
            type_str += f" ({', '.join(repr(v) for v in schema['enum'])})"
        if "default" in schema:
            type_str += f" *(default: `{schema['default']}`)*"
        rows.append(
            f"| `{p['name']}` | {p.get('in', '?')} | {'yes' if p.get('required') else 'no'} | "
            f"{type_str} | {p.get('description', '')} |"
        )
    return "\n".join(rows) + "\n"


def _request_body_summary(body: dict, components: dict) -> str:
    if not body:
        return ""
    content = body.get("content", {})
    json_payload = content.get("application/json")
    if not json_payload:
        return ""
    schema = json_payload.get("schema", {})
    name = _format_schema_name(schema, components)
    required = "**required**" if body.get("required") else "optional"
    return f"**Request body** ({required}): `{name}`\n\n"


def _responses_section(responses: dict, components: dict) -> str:
    if not responses:
        return ""
    out = ["**Responses**", "", "| Status | Body | Description |", "|--------|------|-------------|"]
    for status, body in sorted(responses.items()):
        content = (body or {}).get("content") or {}
        # Prefer application/json, then any content type
        json_payload = content.get("application/json") or next(iter(content.values()), {})
        schema_name = _format_schema_name((json_payload or {}).get("schema", {}), components)
        out.append(f"| `{status}` | `{schema_name}` | {(body or {}).get('description', '')} |")
    out.append("")
    return "\n".join(out)


def _schema_block(name: str, schema: dict, components: dict) -> str:
    """Render a model definition as a properties table."""
    if not schema or "properties" not in schema:
        return ""
    required = set(schema.get("required") or [])
    rows = ["| Field | Type | Required | Description |", "|-------|------|----------|-------------|"]
    for prop_name, prop in schema["properties"].items():
        type_str = _format_schema_name(prop, components)
        if prop.get("enum"):
            type_str += f" ({', '.join(repr(v) for v in prop['enum'])})"
        if "default" in prop:
            type_str += f" *(default: `{prop['default']}`)*"
        desc = prop.get("description", "") or prop.get("title", "")
        rows.append(
            f"| `{prop_name}` | {type_str} | {'yes' if prop_name in required else 'no'} | {desc} |"
        )
    return f"### `{name}`\n\n" + "\n".join(rows) + "\n"


def render() -> str:
    spec = app.openapi()
    info = spec.get("info", {})
    components = spec.get("components", {})

    out: list[str] = []
    out.append(f"# {info.get('title', 'API')}")
    out.append("")
    out.append(
        f"**Auto-generated** from the FastAPI OpenAPI spec — re-run "
        f"`python scripts/generate_api_docs.py` after route changes."
    )
    out.append("")
    out.append(f"- **Version**: {info.get('version', 'unknown')}")
    out.append(f"- **Live spec**: `GET /openapi.json`")
    out.append(f"- **Swagger UI**: `/docs` (when the backend is running)")
    out.append("")
    if info.get("description"):
        out.append(info["description"])
        out.append("")

    # Group operations by tag
    operations_by_tag: dict[str, list[tuple[str, str, dict]]] = {}
    for path, methods in spec.get("paths", {}).items():
        for method, op in methods.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            tags = op.get("tags") or ["misc"]
            for tag in tags:
                operations_by_tag.setdefault(tag, []).append((method.upper(), path, op))

    out.append("## Endpoints")
    out.append("")

    for tag in sorted(operations_by_tag):
        out.append(f"### {tag}")
        out.append("")
        for method, path, op in sorted(operations_by_tag[tag], key=lambda x: (x[1], x[0])):
            summary = op.get("summary") or op.get("operationId") or ""
            out.append(f"#### `{method} {path}`")
            out.append("")
            if summary and summary.lower() != f"{method.lower()}_{path.replace('/', '_')}":
                out.append(f"_{summary}_")
                out.append("")
            if op.get("description"):
                out.append(op["description"])
                out.append("")

            params = op.get("parameters") or []
            params_table = _params_table(params)
            if params_table:
                out.append("**Parameters**\n")
                out.append(params_table)

            body_summary = _request_body_summary(op.get("requestBody") or {}, components)
            if body_summary:
                out.append(body_summary)

            responses_section = _responses_section(op.get("responses") or {}, components)
            if responses_section:
                out.append(responses_section)

            out.append("---")
            out.append("")

    # Schemas
    schemas = (components.get("schemas") or {})
    if schemas:
        out.append("## Schemas")
        out.append("")
        for name in sorted(schemas):
            block = _schema_block(name, schemas[name], components)
            if block:
                out.append(block)

    return "\n".join(out).rstrip() + "\n"


def main() -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(render(), encoding="utf-8")
    print(f"Wrote {OUT_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
