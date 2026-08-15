#!/usr/bin/env python3
"""Walk the codebase and regenerate the living architecture docs.

Outputs (both deterministic — no timestamps in file content so the CI
drift check has something stable to compare against):

  docs/ARCHITECTURE.md            — hierarchical reference, grouped by
                                    functional area, with collapsible
                                    per-module sections
  docs/architecture-diagram.html  — interactive single-file diagram with
                                    search + click-to-expand cards

Run:
  python3 scripts/generate_architecture_docs.py

CI re-runs this and `git diff --exit-code` over the two files to catch
drift between code and docs.
"""

from __future__ import annotations

import ast
import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend" / "app"
FRONTEND = REPO / "frontend" / "src"
MAIN = BACKEND / "main.py"
OUT_MD = REPO / "docs" / "ARCHITECTURE.md"
OUT_HTML = REPO / "docs" / "architecture-diagram.html"


# ---------------------------------------------------------------------------
# Functional grouping. Each area has a list of glob-relative path prefixes;
# modules are assigned to the first area that matches. The `extensions` list
# is hand-curated forward-looking work — explicit so the doc reads as
# roadmap-aware, not just a snapshot of today.
# ---------------------------------------------------------------------------
AREAS: list[dict] = [
    {
        "key": "vm",
        "title": "VM Inventory Management",
        "summary": "Enrolling, listing, editing, and deleting VMs.",
        "match": [
            "backend/app/api/vms.py",
            "backend/app/schemas/vm.py",
            "backend/app/models/vm.py",
        ],
        "extensions": [
            "Windows VM enrollment via WinRM (currently Linux SSH only)",
            "Bulk update endpoint for tagging/labeling VMs at scale",
            "vCenter discovery integration to auto-populate inventory",
        ],
    },
    {
        "key": "baseline",
        "title": "Baseline Collection",
        "summary": "SSH state capture, on-demand triggers, scheduled cadence.",
        "match": [
            "backend/app/core/ssh.py",
            "backend/app/core/capture.py",
            "backend/app/core/scheduler.py",
            "backend/app/core/baseline.py",
            "backend/app/api/snapshots.py",
        ],
        "extensions": [
            "WinRMCollector class parallel to SSHCollector",
            "OS-aware command dispatch (RHEL 7/8/9/10 deltas in systemctl, ss, etc.)",
            "Differential baselines (only collect what changed since last snapshot)",
        ],
    },
    {
        "key": "planning",
        "title": "Migration Planning",
        "summary": "LLM-driven wave planning + MTV/Forklift YAML generation.",
        "match": [
            "backend/app/core/planner.py",
            "backend/app/core/mtv.py",
            "backend/app/api/plans.py",
            "backend/app/schemas/plan.py",
            "backend/app/models/plan.py",
        ],
        "extensions": [
            "Per-wave Ansible playbook generation alongside MTV YAML",
            "Cold migration mode toggle (currently warm-only)",
            "Multi-cluster destination support in MTV Plan output",
        ],
    },
    {
        "key": "validation",
        "title": "Validation Engine",
        "summary": "Pre/post diffing, LLM verdict reasoning, finding triage.",
        "match": [
            "backend/app/core/llm.py",
            "backend/app/models/validation.py",
            "backend/app/schemas/validation.py",
        ],
        "extensions": [
            "Per-finding remediation playbook output",
            "Pluggable LLM backend (vLLM, llama.cpp) alongside Ollama",
            "Confidence scoring on verdicts",
        ],
    },
    {
        "key": "reports",
        "title": "Reporting",
        "summary": "Wave reports, PDF rendering, exportable templates.",
        "match": [
            "backend/app/core/reporter.py",
            "backend/app/api/templates.py",
            "backend/app/schemas/report.py",
        ],
        "extensions": [
            "Email PDF reports to stakeholder lists on wave completion",
            "Slack/webhook notification integrations on verdict change",
            "Customer-supplied Jinja templates for branded PDFs",
        ],
    },
    {
        "key": "audit",
        "title": "Auth & Audit",
        "summary": "Append-only audit trail; future home for RBAC + OIDC.",
        "match": [
            "backend/app/core/audit.py",
            "backend/app/middleware/audit.py",
            "backend/app/models/audit.py",
            "backend/app/api/audit.py",
            "backend/app/schemas/audit.py",
        ],
        "extensions": [
            "OIDC sign-in (currently anonymous behind reverse proxy)",
            "Role-based access control (RBAC) gating mutating routes",
            "Git artifact sync — every generated YAML/PDF committed to a customer-owned repo",
        ],
    },
    {
        "key": "system",
        "title": "System Operations",
        "summary": "Settings, scheduler config, health probes, app bootstrap.",
        "match": [
            "backend/app/main.py",
            "backend/app/core/config.py",
            "backend/app/core/db.py",
            "backend/app/api/settings.py",
            "backend/app/api/health.py",
            "backend/app/schemas/settings.py",
            "backend/app/models/settings.py",
        ],
        "extensions": [
            "Encrypted SSH key vault (currently filesystem-backed Ed25519)",
            "Per-tenant settings (multi-tenant via OCP namespaces)",
            "Helm chart + ArgoCD manifests for OpenShift-native deployment",
        ],
    },
    {
        "key": "frontend",
        "title": "Frontend Components",
        "summary": "React dashboard — inventory, validation, planning, audit.",
        "match": ["frontend/src/"],
        "extensions": [
            "Visual pipeline view (discover → baseline → plan → migrate → validate → report)",
            "Multi-day baseline trend charts",
            "Live SSE feed of validation runs replacing the current polling",
        ],
    },
]


# ---------------------------------------------------------------------------
# Module category → color (drives the HTML legend + card border).
# ---------------------------------------------------------------------------
def category_for(rel_path: str) -> str:
    if rel_path.startswith("backend/app/api/") or rel_path == "backend/app/main.py":
        return "api"
    if rel_path.startswith("backend/app/middleware/"):
        return "api"
    if rel_path.startswith(("backend/app/models/", "backend/app/schemas/")):
        return "data"
    # External integrations: anything that crosses the appliance boundary.
    if rel_path in {
        "backend/app/core/ssh.py",
        "backend/app/core/llm.py",
        "backend/app/core/mtv.py",
        "backend/app/core/scheduler.py",
        "backend/app/core/reporter.py",
    }:
        return "integration"
    if rel_path.startswith("backend/app/core/"):
        return "logic"
    if rel_path.startswith("frontend/src/"):
        return "frontend"
    return "other"


CATEGORY_LABELS = {
    "api": "API endpoints",
    "logic": "Business logic",
    "data": "Data models / schemas",
    "integration": "External integration",
    "frontend": "Frontend component",
    "other": "Other",
}
# Swatch colors for the category legend. Chosen to stay legible on the
# light ground; the previous set was picked against #07070f and several
# (notably #00ff88 / #22ddcc) vanish on white.
CATEGORY_COLORS = {
    "api": "#14496B",
    "logic": "#1F6B3B",
    "data": "#7A4E00",
    "integration": "#4C3A78",
    "frontend": "#0F6E6E",
    "other": "#5F6875",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class Method:
    name: str
    args: list[str]
    summary: str = ""
    is_route: bool = False
    route_method: str = ""
    route_path: str = ""


@dataclass
class Klass:
    name: str
    bases: list[str]
    summary: str
    methods: list[Method]
    is_model: bool = False
    is_schema: bool = False
    fields: list[str] = field(default_factory=list)
    table_name: str = ""


@dataclass
class Function:
    name: str
    args: list[str]
    summary: str
    is_route: bool = False
    route_method: str = ""
    route_path: str = ""


@dataclass
class Module:
    rel_path: str
    summary: str
    classes: list[Klass]
    functions: list[Function]
    imports_internal: list[str]
    category: str

    @property
    def name(self) -> str:
        return Path(self.rel_path).stem

    @property
    def dotted(self) -> str:
        # backend/app/core/ssh.py → app.core.ssh
        if self.rel_path.startswith("backend/"):
            return self.rel_path[len("backend/") :].replace("/", ".")[: -len(".py")]
        return self.rel_path


@dataclass
class FrontendComponent:
    rel_path: str
    name: str
    kind: str  # "component" | "hook" | "helper"
    summary: str
    api_calls: list[str]
    category: str = "frontend"


# ---------------------------------------------------------------------------
# Backend: AST walk
# ---------------------------------------------------------------------------
def _docstring_first_line(node: ast.AST) -> str:
    doc = ast.get_docstring(node) or ""
    return doc.strip().splitlines()[0].strip() if doc.strip() else ""


def _is_public(name: str) -> bool:
    return not name.startswith("_")


def _format_args(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    args = []
    for a in node.args.args:
        args.append(a.arg)
    if node.args.vararg:
        args.append("*" + node.args.vararg.arg)
    if node.args.kwarg:
        args.append("**" + node.args.kwarg.arg)
    return args


# Map of router-variable-name → URL prefix. Filled by parsing main.py.
def _route_prefixes_from_main() -> dict[str, str]:
    if not MAIN.exists():
        return {}
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    prefixes: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # app.include_router(<varname>, prefix="/api/...")
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "include_router"
        ):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Name):
            continue
        prefix = ""
        for kw in node.keywords:
            if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                prefix = kw.value.value
        prefixes[first.id] = prefix
    return prefixes


# Map alias → router-variable-name (e.g., "router" → "vms_router"
# when the file does `from app.api.vms import router as vms_router`).
def _router_alias_map() -> dict[str, dict[str, str]]:
    """Per-module mapping: {module_dotted: {local_router_var: external_alias}}."""
    if not MAIN.exists():
        return {}
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    out: dict[str, dict[str, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        # from app.api.vms import router as vms_router
        if not (node.module and node.module.startswith("app.")):
            continue
        for alias in node.names:
            out.setdefault(node.module, {})[alias.name] = alias.asname or alias.name
    return out


def _extract_route(decorator: ast.expr, valid_router_vars: set[str]) -> tuple[str, str] | None:
    """If decorator is `@<router>.<method>(<path>, ...)`, return (METHOD, path)."""
    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    if not (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in valid_router_vars
    ):
        return None
    method = func.attr.upper()
    if method not in {"GET", "POST", "PATCH", "PUT", "DELETE", "HEAD", "OPTIONS"}:
        return None
    path = ""
    if decorator.args and isinstance(decorator.args[0], ast.Constant):
        path = decorator.args[0].value
    return method, path


def _router_vars_in_module(tree: ast.Module) -> set[str]:
    """Names assigned `APIRouter(...)` at module top level."""
    vars_: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if not isinstance(node.value, ast.Call):
                continue
            f = node.value.func
            if (isinstance(f, ast.Name) and f.id == "APIRouter") or (
                isinstance(f, ast.Attribute) and f.attr == "APIRouter"
            ):
                vars_.add(target.id)
    return vars_


def _extract_class_fields(cls: ast.ClassDef) -> tuple[list[str], str]:
    """Return (field names, table_name if SQLAlchemy model)."""
    fields_: list[str] = []
    table_name = ""
    for stmt in cls.body:
        # __tablename__ = "..."
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "__tablename__"
                    and isinstance(stmt.value, ast.Constant)
                ):
                    table_name = stmt.value.value
        # `name: Mapped[str] = mapped_column(...)` or `name: str = Field(...)`
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name = stmt.target.id
            if _is_public(name):
                fields_.append(name)
    return fields_, table_name


def _bases_of(cls: ast.ClassDef) -> list[str]:
    out = []
    for base in cls.bases:
        if isinstance(base, ast.Name):
            out.append(base.id)
        elif isinstance(base, ast.Attribute):
            out.append(base.attr)
    return out


def parse_backend() -> list[Module]:
    prefixes = _route_prefixes_from_main()
    aliases = _router_alias_map()

    modules: list[Module] = []
    for py_path in sorted(BACKEND.rglob("*.py")):
        if py_path.name == "__init__.py" and py_path.read_text(encoding="utf-8").strip() == "":
            continue
        rel = str(py_path.relative_to(REPO))
        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        dotted = rel[len("backend/") :].replace("/", ".")[: -len(".py")]
        module_router_vars = _router_vars_in_module(tree)

        # Build prefix lookup for routes in THIS module: local var → prefix.
        local_prefix: dict[str, str] = {}
        for var in module_router_vars:
            external = aliases.get(dotted, {}).get(var, var)
            local_prefix[var] = prefixes.get(external, "")

        classes: list[Klass] = []
        functions: list[Function] = []
        imports_internal: list[str] = []

        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("app."):
                    imports_internal.append(node.module)

            if isinstance(node, ast.ClassDef):
                methods: list[Method] = []
                for stmt in node.body:
                    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if not _is_public(stmt.name):
                            continue
                        methods.append(
                            Method(
                                name=stmt.name,
                                args=_format_args(stmt),
                                summary=_docstring_first_line(stmt),
                            )
                        )
                bases = _bases_of(node)
                fields_, table_name = _extract_class_fields(node)
                is_model = bool(table_name)
                is_schema = "BaseModel" in bases
                classes.append(
                    Klass(
                        name=node.name,
                        bases=bases,
                        summary=_docstring_first_line(node),
                        methods=methods,
                        is_model=is_model,
                        is_schema=is_schema,
                        fields=fields_,
                        table_name=table_name,
                    )
                )

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not _is_public(node.name):
                    continue
                route: tuple[str, str] | None = None
                for dec in node.decorator_list:
                    found = _extract_route(dec, module_router_vars)
                    if found:
                        # Pull the prefix attached to the router variable.
                        for var in module_router_vars:
                            if (
                                isinstance(dec, ast.Call)
                                and isinstance(dec.func, ast.Attribute)
                                and isinstance(dec.func.value, ast.Name)
                                and dec.func.value.id == var
                            ):
                                full_path = (local_prefix.get(var, "") + found[1]) or "/"
                                route = (found[0], full_path)
                                break
                        else:
                            route = found
                        break
                functions.append(
                    Function(
                        name=node.name,
                        args=_format_args(node),
                        summary=_docstring_first_line(node),
                        is_route=route is not None,
                        route_method=route[0] if route else "",
                        route_path=route[1] if route else "",
                    )
                )

        modules.append(
            Module(
                rel_path=rel,
                summary=_docstring_first_line(tree),
                classes=classes,
                functions=functions,
                imports_internal=sorted(set(imports_internal)),
                category=category_for(rel),
            )
        )
    return modules


# ---------------------------------------------------------------------------
# Frontend: regex walk (JSX AST is more trouble than it's worth here)
# ---------------------------------------------------------------------------
_RE_FUNCTION = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_][\w]*)\s*\(",
    re.M,
)
_RE_ARROW = re.compile(r"^\s*(?:export\s+)?const\s+([A-Z][\w]*)\s*=\s*\(", re.M)
_RE_HOOK = re.compile(r"^\s*(?:export\s+)?(?:function|const)\s+(use[A-Z]\w*)\s*[=(]", re.M)
_RE_FETCH = re.compile(r"""(?:fetchJSON|fetch)\(\s*[`"']([^`"']*?/api/[^`"']*?)[`"']""")
_RE_DOC_FIRST = re.compile(r"^\s*//\s*(.+)$", re.M)


def _strip_path_template(path: str) -> str:
    """Normalize `\\${vmId}` interpolations down to `{id}` so the diagram
    isn't full of literal template-string fragments."""
    return re.sub(r"\$\{[^}]+\}", "{id}", path)


def parse_frontend() -> list[FrontendComponent]:
    components: list[FrontendComponent] = []
    if not FRONTEND.exists():
        return components

    for src in sorted(FRONTEND.rglob("*.jsx")) + sorted(FRONTEND.rglob("*.js")):
        if "node_modules" in src.parts or "dist" in src.parts:
            continue
        rel = str(src.relative_to(REPO))
        text = src.read_text(encoding="utf-8")

        api_calls = sorted({_strip_path_template(m) for m in _RE_FETCH.findall(text)})

        # File-level doc heuristic: first single-line comment in the file.
        first_comment_match = _RE_DOC_FIRST.search(text[: 2000])
        file_summary = first_comment_match.group(1).strip() if first_comment_match else ""

        names: list[tuple[str, str]] = []  # (name, kind)
        for m in _RE_FUNCTION.finditer(text):
            name = m.group(1)
            if name.startswith("use") and len(name) > 3 and name[3].isupper():
                names.append((name, "hook"))
            elif name[0].isupper():
                names.append((name, "component"))
            else:
                names.append((name, "helper"))
        for m in _RE_ARROW.finditer(text):
            names.append((m.group(1), "component"))
        # Deduplicate while preserving order.
        seen = set()
        deduped = []
        for n, k in names:
            if n in seen:
                continue
            seen.add(n)
            deduped.append((n, k))

        if not deduped:
            continue

        # Top-level (default export / canonical) component is the first
        # capitalized name in the file. We attach api_calls + summary to
        # the file's "primary" entry, and list the rest as siblings.
        primary_name, primary_kind = deduped[0]
        components.append(
            FrontendComponent(
                rel_path=rel,
                name=primary_name,
                kind=primary_kind,
                summary=file_summary,
                api_calls=api_calls,
            )
        )
        for nm, kd in deduped[1:]:
            components.append(
                FrontendComponent(rel_path=rel, name=nm, kind=kd, summary="", api_calls=[])
            )
    return components


# ---------------------------------------------------------------------------
# Area assignment
# ---------------------------------------------------------------------------
def assign_area(rel_path: str) -> str:
    for area in AREAS:
        for prefix in area["match"]:
            if rel_path == prefix or rel_path.startswith(prefix.rstrip("/") + "/"):
                return area["key"]
            if rel_path.startswith(prefix):
                return area["key"]
    return "system"  # safe default — anything unmatched lands in system ops


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------
def render_markdown(modules: list[Module], components: list[FrontendComponent]) -> str:
    by_area: dict[str, list[Module | FrontendComponent]] = {a["key"]: [] for a in AREAS}
    for m in modules:
        by_area[assign_area(m.rel_path)].append(m)
    for c in components:
        by_area[assign_area(c.rel_path)].append(c)

    lines: list[str] = [
        "# VirtValidate Architecture",
        "",
        "Living reference for every module, class, function, and React component in",
        "the codebase. Generated by `scripts/generate_architecture_docs.py` — do not",
        "hand-edit; run the script and commit the diff.",
        "",
        "See also:",
        "",
        "- [`docs/architecture-diagram.html`](architecture-diagram.html) — interactive diagram of the same data",
        "- [`docs/product-map.html`](product-map.html) — what the product does",
        "- [`docs/API.md`](API.md) — REST endpoint reference (auto-generated from OpenAPI)",
        "",
        "## Color legend",
        "",
        "| Category | Meaning |",
        "|---|---|",
    ]
    for cat, label in CATEGORY_LABELS.items():
        if cat == "other":
            continue
        lines.append(f"| `{cat}` | {label} |")

    lines.append("")
    lines.append("## Functional areas")
    lines.append("")
    for area in AREAS:
        members = by_area.get(area["key"], [])
        if not members:
            continue
        lines.append(f"### {area['title']}")
        lines.append("")
        lines.append(area["summary"])
        lines.append("")

        backend_mods = [m for m in members if isinstance(m, Module)]
        frontend_mods_grouped: dict[str, list[FrontendComponent]] = {}
        for c in members:
            if isinstance(c, FrontendComponent):
                frontend_mods_grouped.setdefault(c.rel_path, []).append(c)

        for mod in backend_mods:
            lines.extend(_render_module_md(mod))

        for path, comps in frontend_mods_grouped.items():
            lines.extend(_render_frontend_md(path, comps))

        lines.append(f"**Planned extensions for {area['title']}:**")
        lines.append("")
        for ext in area["extensions"]:
            lines.append(f"- {ext}")
        lines.append("")

    lines.append("## How to extend")
    lines.append("")
    lines.append(
        "When you add code that should appear in this document, just regenerate it:"
    )
    lines.append("")
    lines.append("```bash")
    lines.append("python3 scripts/generate_architecture_docs.py")
    lines.append("```")
    lines.append("")
    lines.append(
        "The walker recognizes a few patterns; place new code so it fits one of them:"
    )
    lines.append("")
    lines.append(
        "- **New API endpoint:** add a function in a `backend/app/api/*.py` file with a"
        " `@router.<method>(...)` decorator. The walker resolves the prefix from"
        " `app/main.py:include_router(..., prefix=...)` automatically."
    )
    lines.append(
        "- **New SQLAlchemy model:** declare `__tablename__` on the class — the walker"
        " infers `is_model=True` and lists declared columns."
    )
    lines.append(
        "- **New Pydantic schema:** subclass `BaseModel`. Annotated assignments"
        " (`name: str = Field(...)`) become listed fields."
    )
    lines.append(
        "- **New functional area:** add a new entry to `AREAS` in"
        " `scripts/generate_architecture_docs.py` (path globs + planned extensions)."
    )
    lines.append(
        "- **New React component:** standard `function ComponentName(...)` or"
        " `const ComponentName = (...) =>` syntax is detected. `fetchJSON(\"/api/...\")`"
        " calls are extracted as the component's API surface."
    )
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_module_md(mod: Module) -> list[str]:
    cat_label = CATEGORY_LABELS.get(mod.category, mod.category)
    lines = [
        f"<details><summary><strong><code>{mod.dotted}</code></strong>"
        f" — <em>{cat_label}</em>"
        f"{' · ' + html.escape(mod.summary) if mod.summary else ''}</summary>",
        "",
        f"Path: `{mod.rel_path}`  ",
    ]
    if mod.imports_internal:
        lines.append(
            "Depends on: " + ", ".join(f"`{m}`" for m in mod.imports_internal)
        )
    lines.append("")

    routes = [f for f in mod.functions if f.is_route]
    plain_funcs = [f for f in mod.functions if not f.is_route]

    if routes:
        lines.append("**Routes**")
        lines.append("")
        lines.append("| Method | Path | Handler | Purpose |")
        lines.append("|---|---|---|---|")
        for r in routes:
            summary = html.escape(r.summary) if r.summary else "—"
            lines.append(
                f"| `{r.route_method}` | `{r.route_path}` | `{r.name}({', '.join(r.args)})` | {summary} |"
            )
        lines.append("")

    if mod.classes:
        lines.append("**Classes**")
        lines.append("")
        for cls in mod.classes:
            kind_bits = []
            if cls.is_model:
                kind_bits.append(f"SQLAlchemy model · table `{cls.table_name}`")
            if cls.is_schema:
                kind_bits.append("Pydantic schema")
            kind_str = " · ".join(kind_bits) if kind_bits else "Class"
            lines.append(f"- **`{cls.name}`** ({kind_str})")
            if cls.summary:
                lines.append(f"  - {cls.summary}")
            if cls.fields:
                lines.append(
                    f"  - Fields: {', '.join('`' + f + '`' for f in cls.fields)}"
                )
            if cls.methods:
                lines.append("  - Methods:")
                for m in cls.methods:
                    sig = f"`{m.name}({', '.join(m.args)})`"
                    suffix = f" — {m.summary}" if m.summary else ""
                    lines.append(f"    - {sig}{suffix}")
        lines.append("")

    if plain_funcs:
        lines.append("**Functions**")
        lines.append("")
        for f in plain_funcs:
            sig = f"`{f.name}({', '.join(f.args)})`"
            suffix = f" — {f.summary}" if f.summary else ""
            lines.append(f"- {sig}{suffix}")
        lines.append("")

    lines.append("</details>")
    lines.append("")
    return lines


def _render_frontend_md(path: str, comps: list[FrontendComponent]) -> list[str]:
    primary = comps[0]
    lines = [
        f"<details><summary><strong><code>{path}</code></strong>"
        " — <em>Frontend component</em>"
        f"{' · ' + html.escape(primary.summary) if primary.summary else ''}</summary>",
        "",
    ]
    if primary.api_calls:
        lines.append("API calls:")
        for c in primary.api_calls:
            lines.append(f"- `{c}`")
        lines.append("")
    lines.append("Exports / inner components:")
    for c in comps:
        kind_label = {"hook": "hook", "component": "component", "helper": "helper"}[c.kind]
        lines.append(f"- **`{c.name}`** ({kind_label})")
    lines.append("")
    lines.append("</details>")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------------------
def render_html(modules: list[Module], components: list[FrontendComponent]) -> str:
    # Pre-compute area → modules + frontend cards.
    cards_by_area: dict[str, list[dict]] = {a["key"]: [] for a in AREAS}

    for m in modules:
        cards_by_area[assign_area(m.rel_path)].append(_module_to_card(m))
    fe_by_path: dict[str, list[FrontendComponent]] = {}
    for c in components:
        fe_by_path.setdefault(c.rel_path, []).append(c)
    for path, comps in fe_by_path.items():
        cards_by_area[assign_area(path)].append(_frontend_to_card(path, comps))

    # JSON payload for client-side filtering.
    data_payload = {
        "areas": AREAS,
        "cards": cards_by_area,
        "categories": [
            {"key": k, "label": CATEGORY_LABELS[k], "color": CATEGORY_COLORS[k]}
            for k in CATEGORY_LABELS
            if k != "other"
        ],
    }

    return _HTML_TEMPLATE.replace(
        "/*__DATA__*/", json.dumps(data_payload, indent=2, sort_keys=False)
    )


def _module_to_card(mod: Module) -> dict:
    routes = [
        {
            "method": f.route_method,
            "path": f.route_path,
            "handler": f.name,
            "summary": f.summary,
        }
        for f in mod.functions
        if f.is_route
    ]
    classes = []
    for cls in mod.classes:
        classes.append(
            {
                "name": cls.name,
                "kind": (
                    "model" if cls.is_model else "schema" if cls.is_schema else "class"
                ),
                "table": cls.table_name,
                "fields": cls.fields,
                "methods": [{"name": m.name, "summary": m.summary} for m in cls.methods],
                "summary": cls.summary,
            }
        )
    funcs = [
        {"name": f.name, "summary": f.summary}
        for f in mod.functions
        if not f.is_route
    ]
    return {
        "kind": "module",
        "id": mod.dotted,
        "title": mod.dotted,
        "subtitle": mod.rel_path,
        "summary": mod.summary,
        "category": mod.category,
        "routes": routes,
        "classes": classes,
        "functions": funcs,
        "depends_on": mod.imports_internal,
    }


def _frontend_to_card(path: str, comps: list[FrontendComponent]) -> dict:
    primary = comps[0]
    return {
        "kind": "frontend",
        "id": path,
        "title": Path(path).name,
        "subtitle": path,
        "summary": primary.summary,
        "category": "frontend",
        "api_calls": primary.api_calls,
        "members": [
            {"name": c.name, "kind": c.kind} for c in comps
        ],
    }


# Note: the HTML template lives as a single triple-quoted string. The data
# payload gets injected via `/*__DATA__*/`. Single-file output keeps the
# diagram trivially shareable (no external assets required).
_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>VirtValidate · Architecture Diagram</title>
<style>
  /* ---- Tokens ------------------------------------------------------
     Same three-block theming contract as docs/product-map.html and the
     other hand-written docs. No web fonts: this product ships into
     air-gapped sites, so the page must not fetch anything. ----------- */
  :root {
    --ground:    #EDEFF2;
    --surface:   #FFFFFF;
    --surface-2: #F4F6F8;
    --rule:      #D3D8DF;
    --rule-hard: #B3BBC6;
    --ink:       #151A21;
    --ink-2:     #39414E;
    --muted:     #5F6875;
    --accent:      #A6120D;
    --accent-soft: #FBEAE9;
    --accent-rule: #E4B4B1;

    --get:    #14496B;  --get-bg:    #E2EEF6;
    --post:   #1F6B3B;  --post-bg:   #E4F2E8;
    --mutate: #7A4E00;  --mutate-bg: #FAF0DC;
    --delete: #8C2018;  --delete-bg: #FBEAE9;

    --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  }

  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --ground:    #16181C;
      --surface:   #1E2126;
      --surface-2: #262A30;
      --rule:      #343941;
      --rule-hard: #4A515B;
      --ink:       #EDEFF2;
      --ink-2:     #C3C9D2;
      --muted:     #949BA6;
      --accent:      #F09A95;
      --accent-soft: #3A1E1C;
      --accent-rule: #5E302D;
      --get:    #8FC2E4;  --get-bg:    #172833;
      --post:   #7BC99A;  --post-bg:   #1B2E22;
      --mutate: #E0B770;  --mutate-bg: #322817;
      --delete: #F09A95;  --delete-bg: #3A1E1C;
    }
  }

  :root[data-theme="dark"] {
    --ground:    #16181C;
    --surface:   #1E2126;
    --surface-2: #262A30;
    --rule:      #343941;
    --rule-hard: #4A515B;
    --ink:       #EDEFF2;
    --ink-2:     #C3C9D2;
    --muted:     #949BA6;
    --accent:      #F09A95;
    --accent-soft: #3A1E1C;
    --accent-rule: #5E302D;
    --get:    #8FC2E4;  --get-bg:    #172833;
    --post:   #7BC99A;  --post-bg:   #1B2E22;
    --mutate: #E0B770;  --mutate-bg: #322817;
    --delete: #F09A95;  --delete-bg: #3A1E1C;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--ground); color: var(--ink); font-family: var(--sans);
         line-height: 1.55; -webkit-font-smoothing: antialiased; }
  a { color: var(--accent); }
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-track { background: var(--surface-2); }
  ::-webkit-scrollbar-thumb { background: var(--rule-hard); border-radius: 999px; }

  .nav { position: sticky; top: 0; z-index: 100; padding: 13px 32px;
         background: color-mix(in srgb, var(--surface) 92%, transparent);
         backdrop-filter: blur(10px); border-bottom: 1px solid var(--rule);
         display: flex; align-items: center; justify-content: space-between; gap: 24px; flex-wrap: wrap; }
  .nav-left { display: flex; align-items: center; gap: 14px; }
  .nav-tri { width: 10px; height: 10px; border-radius: 3px; background: var(--accent); }
  .nav-title { font-size: 15px; font-weight: 700; letter-spacing: -0.01em; }
  .nav-subtitle { font-family: var(--mono); font-size: 11px; color: var(--muted); margin-top: 1px; }
  .nav-link { font-size: 12.5px; color: var(--ink-2); text-decoration: none;
              padding: 5px 10px; border-radius: 6px; }
  .nav-link:hover { color: var(--accent); background: var(--surface-2); }

  .hero { max-width: 1280px; margin: 0 auto; padding: 44px 32px 20px; }
  .hero-eyebrow { font-family: var(--mono); font-size: 11.5px; color: var(--muted);
                  letter-spacing: 0.13em; margin-bottom: 10px; text-transform: uppercase; }
  .hero h1 { font-size: 32px; line-height: 1.18; letter-spacing: -0.02em; font-weight: 700; }
  .hero p { color: var(--ink-2); margin-top: 12px; max-width: 78ch; }

  .controls { position: sticky; top: 57px; z-index: 90; display: flex; gap: 12px;
              align-items: center; flex-wrap: wrap; padding: 14px 32px;
              border-top: 1px solid var(--rule); border-bottom: 1px solid var(--rule);
              background: var(--surface); }
  .search { flex: 1; min-width: 240px; background: var(--surface-2);
            border: 1px solid var(--rule); border-radius: 8px; color: var(--ink);
            padding: 9px 12px; font-family: var(--sans); font-size: 14px; outline: none; }
  .search:focus { border-color: var(--accent-rule); }
  .legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--muted); }
  .legend-item { display: flex; align-items: center; gap: 6px; }
  .legend-swatch { width: 9px; height: 9px; border-radius: 999px; display: inline-block; }

  .areas { max-width: 1280px; margin: 0 auto; padding: 26px 32px 60px; }
  .area { margin-bottom: 34px; }
  .area-title { font-size: 13px; font-weight: 700; letter-spacing: 0.04em;
                text-transform: uppercase; padding-bottom: 8px; margin-bottom: 6px;
                border-bottom: 1px solid var(--rule); }
  .area-summary { color: var(--muted); font-size: 13.5px; margin-bottom: 14px; max-width: 80ch; }

  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 12px; }
  .card { border: 1px solid var(--rule); background: var(--surface);
          border-radius: 10px; padding: 16px; transition: border-color 0.12s; }
  .card:hover { border-color: var(--rule-hard); }
  .card-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
  .card-id { font-family: var(--mono); font-size: 13.5px; color: var(--ink); font-weight: 600; word-break: break-word; }
  .card-sub { font-family: var(--mono); font-size: 11px; color: var(--muted); margin-top: 3px; word-break: break-all; }
  .card-tag { font-family: var(--mono); font-size: 10px; letter-spacing: 0.06em;
              padding: 3px 8px; border-radius: 999px; white-space: nowrap;
              background: var(--surface-2); color: var(--muted); border: 1px solid var(--rule); }
  .card-summary { color: var(--ink-2); font-size: 13.5px; margin-top: 10px; }
  .card-section { margin-top: 12px; }
  .row { padding: 6px 0; border-bottom: 1px solid var(--rule);
         font-family: var(--mono); font-size: 12px; color: var(--ink-2); }
  .row:last-child { border-bottom: none; }
  .kind { color: var(--muted); }
  .path { font-family: var(--mono); color: var(--ink-2); word-break: break-all; }
  .meta { font-family: var(--mono); font-size: 11px; color: var(--muted); }

  .method { font-family: var(--mono); font-size: 10px; letter-spacing: 0.05em;
            padding: 2px 7px; border-radius: 999px; margin-right: 7px; display: inline-block; }
  .method-GET { background: var(--get-bg); color: var(--get); }
  .method-POST { background: var(--post-bg); color: var(--post); }
  .method-PATCH { background: var(--mutate-bg); color: var(--mutate); }
  .method-PUT { background: var(--mutate-bg); color: var(--mutate); }
  .method-DELETE { background: var(--delete-bg); color: var(--delete); }

  .empty { padding: 60px 24px; text-align: center; color: var(--muted); font-size: 14px; }
  .footer { padding: 22px 32px 48px; max-width: 1280px; margin: 0 auto;
            border-top: 1px solid var(--rule); color: var(--muted); font-size: 12.5px; }

  @media (max-width: 760px) {
    .grid { grid-template-columns: 1fr; }
    .hero h1 { font-size: 25px; }
  }
</style>
</head>
<body>

<nav class="nav">
  <div class="nav-left">
    <div class="nav-tri"></div>
    <div>
      <div class="nav-title">VirtValidate · Architecture</div>
      <div class="nav-subtitle">code map · regenerate via scripts/generate_architecture_docs.py</div>
    </div>
  </div>
  <div style="display:flex; gap:10px;">
    <a class="nav-link" href="product-map.html">Product Map</a>
    <a class="nav-link" href="ARCHITECTURE.md">Markdown View</a>
    <a class="nav-link" href="../README.md">Readme</a>
  </div>
</nav>

<header class="hero">
  <div class="hero-eyebrow">how it's built</div>
  <h1>Module &amp; component architecture</h1>
  <p>Every Python module, class, function, API route, and React component the
     codebase exposes — grouped by functional area. Search the box below or
     click any card to expand its members. Categories color-code the cross-
     cutting role each module plays.</p>
</header>

<section class="controls">
  <input id="search" class="search" type="text" autocomplete="off"
         placeholder="filter by module, class, function, route…" />
  <div class="legend" id="legend"></div>
</section>

<main class="areas" id="areas"></main>

<footer class="footer">
  <div>Generated from source — do not hand-edit. Run <code>python3 scripts/generate_architecture_docs.py</code> to regenerate.</div>
</footer>

<script>
const DATA = /*__DATA__*/;
const COLOR = Object.fromEntries(DATA.categories.map(c => [c.key, c.color]));
const LABEL = Object.fromEntries(DATA.categories.map(c => [c.key, c.label]));

// Legend
const legend = document.getElementById("legend");
DATA.categories.forEach(c => {
  const el = document.createElement("span");
  el.className = "legend-item";
  el.innerHTML = `<span class="legend-swatch" style="background:${c.color}"></span>${c.label}`;
  legend.appendChild(el);
});

function renderCard(card) {
  const color = COLOR[card.category] || "var(--muted)";
  const div = document.createElement("article");
  div.className = "card";
  div.style.borderLeft = `3px solid ${color}`;
  div.dataset.search = JSON.stringify(card).toLowerCase();

  const header = document.createElement("div");
  header.className = "card-header";
  header.innerHTML = `
    <div>
      <div class="card-id">${escapeHtml(card.title)}</div>
      <div class="card-sub">${escapeHtml(card.subtitle)}</div>
    </div>
    <span class="card-tag" style="background:${color}22; color:${color}; border:1px solid ${color}55">
      ${escapeHtml(LABEL[card.category] || card.category)}
    </span>
  `;
  div.appendChild(header);

  if (card.summary) {
    const s = document.createElement("div");
    s.className = "card-summary";
    s.textContent = card.summary;
    div.appendChild(s);
  }

  if (card.kind === "module") {
    if ((card.routes || []).length > 0) {
      div.appendChild(makeSection("Routes"));
      card.routes.forEach(r => {
        const row = document.createElement("div");
        row.className = "row";
        row.innerHTML = `<span class="method method-${r.method}">${r.method}</span>` +
                        `<span class="path">${escapeHtml(r.path)}</span>` +
                        `<span class="meta" style="margin-left:auto">${escapeHtml(r.handler)}</span>`;
        div.appendChild(row);
        if (r.summary) {
          const m = document.createElement("div");
          m.className = "meta";
          m.style.padding = "0 0 6px 60px";
          m.textContent = r.summary;
          div.appendChild(m);
        }
      });
    }

    if ((card.classes || []).length > 0) {
      div.appendChild(makeSection("Classes"));
      card.classes.forEach(c => {
        const det = document.createElement("details");
        const sum = document.createElement("summary");
        const kind = c.kind === "model" ? `model · ${c.table}` :
                     c.kind === "schema" ? "schema" : "class";
        sum.innerHTML = `<code style="font-family:var(--mono);color:var(--ink)">${escapeHtml(c.name)}</code> <span style="color:var(--muted)">(${kind})</span>`;
        det.appendChild(sum);
        if (c.summary) det.appendChild(metaLine(c.summary));
        if ((c.fields || []).length > 0) {
          det.appendChild(metaLine("Fields: " + c.fields.join(", ")));
        }
        (c.methods || []).forEach(m => {
          const r = document.createElement("div");
          r.className = "row";
          r.innerHTML = `<span class="path">${escapeHtml(m.name)}()</span>` +
                        (m.summary ? `<span class="meta" style="margin-left:auto">${escapeHtml(m.summary)}</span>` : "");
          det.appendChild(r);
        });
        div.appendChild(det);
      });
    }

    if ((card.functions || []).length > 0) {
      div.appendChild(makeSection("Functions"));
      const det = document.createElement("details");
      det.appendChild(Object.assign(document.createElement("summary"),
        { textContent: `${card.functions.length} function${card.functions.length === 1 ? "" : "s"}` }));
      card.functions.forEach(f => {
        const r = document.createElement("div");
        r.className = "row";
        r.innerHTML = `<span class="path">${escapeHtml(f.name)}()</span>` +
                      (f.summary ? `<span class="meta" style="margin-left:auto">${escapeHtml(f.summary)}</span>` : "");
        det.appendChild(r);
      });
      div.appendChild(det);
    }

    if ((card.depends_on || []).length > 0) {
      div.appendChild(makeSection("Depends on"));
      const wrap = document.createElement("div");
      wrap.style.fontFamily = "var(--mono)";
      wrap.style.fontSize = "11px";
      wrap.style.color = "var(--muted)";
      wrap.style.lineHeight = "1.7";
      wrap.textContent = card.depends_on.join("  ·  ");
      div.appendChild(wrap);
    }
  }

  if (card.kind === "frontend") {
    if ((card.api_calls || []).length > 0) {
      div.appendChild(makeSection("API calls"));
      card.api_calls.forEach(c => {
        const r = document.createElement("div");
        r.className = "row";
        r.innerHTML = `<span class="path">${escapeHtml(c)}</span>`;
        div.appendChild(r);
      });
    }
    if ((card.members || []).length > 0) {
      div.appendChild(makeSection("Exports / inner components"));
      card.members.forEach(m => {
        const r = document.createElement("div");
        r.className = "row";
        r.innerHTML = `<span class="path">${escapeHtml(m.name)}</span>` +
                      `<span class="meta" style="margin-left:auto">${escapeHtml(m.kind)}</span>`;
        div.appendChild(r);
      });
    }
  }

  return div;
}

function makeSection(label) {
  const el = document.createElement("div");
  el.className = "card-section";
  el.textContent = label;
  return el;
}
function metaLine(t) {
  const el = document.createElement("div");
  el.className = "meta";
  el.style.padding = "4px 0";
  el.textContent = t;
  return el;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

const areasEl = document.getElementById("areas");
DATA.areas.forEach(area => {
  const cards = DATA.cards[area.key] || [];
  if (cards.length === 0) return;
  const sec = document.createElement("section");
  sec.className = "area";
  sec.dataset.areaKey = area.key;
  sec.innerHTML = `
    <h2 class="area-title">${escapeHtml(area.title)}</h2>
    <p class="area-summary">${escapeHtml(area.summary)}</p>
  `;
  const grid = document.createElement("div");
  grid.className = "grid";
  cards.forEach(c => grid.appendChild(renderCard(c)));
  sec.appendChild(grid);

  // Planned extensions strip — gives the doc a forward-looking voice.
  if ((area.extensions || []).length > 0) {
    const ext = document.createElement("details");
    ext.style.marginTop = "16px";
    ext.style.padding = "12px 16px";
    ext.style.border = "1px dashed var(--rule-hard)";
    ext.style.background = "var(--surface-2)";
    ext.style.borderRadius = "8px";
    const sum = document.createElement("summary");
    sum.textContent = "Planned extensions";
    ext.appendChild(sum);
    const ul = document.createElement("ul");
    ul.style.paddingLeft = "20px";
    ul.style.marginTop = "10px";
    ul.style.color = "var(--muted)";
    ul.style.fontSize = "13px";
    ul.style.lineHeight = "1.6";
    area.extensions.forEach(e => {
      const li = document.createElement("li");
      li.textContent = e;
      ul.appendChild(li);
    });
    ext.appendChild(ul);
    sec.appendChild(ext);
  }

  areasEl.appendChild(sec);
});

// Live filter — match against the JSON-stringified card content (lowercased).
const search = document.getElementById("search");
search.addEventListener("input", () => {
  const q = search.value.trim().toLowerCase();
  document.querySelectorAll(".area").forEach(area => {
    let visible = 0;
    area.querySelectorAll(".card").forEach(card => {
      const hit = !q || card.dataset.search.includes(q);
      card.classList.toggle("hidden", !hit);
      if (hit) visible += 1;
    });
    area.style.display = visible === 0 && q ? "none" : "";
  });
});
</script>

</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    modules = parse_backend()
    components = parse_frontend()

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(render_markdown(modules, components), encoding="utf-8")
    OUT_HTML.write_text(render_html(modules, components), encoding="utf-8")

    print(f"wrote {OUT_MD.relative_to(REPO)} ({OUT_MD.stat().st_size:,} bytes)")
    print(f"wrote {OUT_HTML.relative_to(REPO)} ({OUT_HTML.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
