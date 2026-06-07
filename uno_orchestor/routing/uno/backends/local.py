"""Local backends for deterministic UNO primitives."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from uno_orchestor.routing.uno.primitives import PrimitiveResult, Route


class LocalPrimitiveBackend:
    name = "local"

    def __init__(
        self,
        exec_timeout_sec: float | None = None,
        output_limit_chars: int | None = None,
        strict_optional_backends: bool | None = None,
    ):
        self.exec_timeout_sec = float(exec_timeout_sec or os.environ.get("UNO_EXEC_TIMEOUT_SEC", "5"))
        self.output_limit_chars = int(output_limit_chars or os.environ.get("UNO_PRIMITIVE_OUTPUT_LIMIT_CHARS", "12000"))
        self.strict_optional_backends = (
            os.environ.get("UNO_STRICT_PRIMITIVE_BACKENDS", "0") == "1"
            if strict_optional_backends is None
            else strict_optional_backends
        )

    def run(self, route: Route, question: str) -> PrimitiveResult | None:
        if route.skill == "execute_python":
            return self._execute_python(route.query)
        if route.skill == "execute_shell":
            return self._execute_shell(route.query)
        if route.skill == "symbolic_math":
            return self._symbolic_math(route.query)
        if route.skill == "extract_field":
            return self._extract_field(route.query, question)
        if route.skill == "parse_structured":
            return self._parse_structured(route.query, question)
        if route.skill == "database_query":
            return self._database_query(route.query)
        if route.skill == "call_api":
            return self._call_api(route.query)
        return None

    def _execute_python(self, query: str) -> PrimitiveResult:
        code = _extract_fenced_block(query, ("python", "py")) or query.strip()
        return self._run_subprocess([sys.executable, "-I", "-c", code], backend="execute_python")

    def _execute_shell(self, query: str) -> PrimitiveResult:
        command = _extract_fenced_block(query, ("bash", "sh", "shell")) or query.strip()
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return PrimitiveResult(
                f"<error reason=\"invalid_shell_command\">{_xml_text(str(exc))}</error>",
                backend="execute_shell",
            )
        return self._run_subprocess(argv, backend="execute_shell")

    def _run_subprocess(self, command: list[str], backend: str) -> PrimitiveResult:
        if not command:
            return PrimitiveResult("<error reason=\"empty_command\"/>", backend=backend)
        env = {
            "HOME": tempfile.gettempdir(),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8",
        }
        with tempfile.TemporaryDirectory(prefix=f"uno-{backend}-") as cwd:
            try:
                proc = subprocess.run(
                    command,
                    cwd=cwd,
                    env=env,
                    text=True,
                    input="",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.exec_timeout_sec,
                )
            except subprocess.TimeoutExpired as exc:
                stdout = self._truncate(exc.stdout or "")
                stderr = self._truncate(exc.stderr or "")
                return PrimitiveResult(
                    f"<error reason=\"timeout\" after_s=\"{self.exec_timeout_sec}\"/>\n"
                    f"<stdout>{_xml_text(stdout)}</stdout>\n"
                    f"<stderr>{_xml_text(stderr)}</stderr>",
                    backend=backend,
                    timed_out=True,
                )
            except Exception as exc:
                return PrimitiveResult(
                    f"<error reason=\"execution_failed\">{_xml_text(str(exc))}</error>",
                    backend=backend,
                )

        stdout = self._truncate(proc.stdout)
        stderr = self._truncate(proc.stderr)
        return PrimitiveResult(
            f"<exit_code>{proc.returncode}</exit_code>\n"
            f"<stdout>{_xml_text(stdout)}</stdout>\n"
            f"<stderr>{_xml_text(stderr)}</stderr>",
            backend=backend,
        )

    def _symbolic_math(self, query: str) -> PrimitiveResult | None:
        try:
            import sympy as sp
            from sympy.parsing.sympy_parser import (
                convert_xor,
                implicit_multiplication_application,
                parse_expr,
                standard_transformations,
            )
        except Exception:
            return None

        payload = _first_json(query)
        try:
            transformations = standard_transformations + (implicit_multiplication_application, convert_xor)
            if isinstance(payload, dict) and payload.get("expression"):
                expression = str(payload["expression"])
                operation = str(payload.get("operation", "simplify")).lower()
                symbol_name = str(payload.get("symbol", "x"))
                symbol = sp.Symbol(symbol_name)
                expr = parse_expr(expression, transformations=transformations)
                if operation in {"simplify", "reduce"}:
                    value = sp.simplify(expr)
                elif operation == "factor":
                    value = sp.factor(expr)
                elif operation == "expand":
                    value = sp.expand(expr)
                elif operation in {"differentiate", "diff", "derivative"}:
                    value = sp.diff(expr, symbol)
                elif operation == "integrate":
                    value = sp.integrate(expr, symbol)
                elif operation == "solve":
                    value = sp.solve(expr, symbol)
                else:
                    return None
                return PrimitiveResult(str(value), backend="symbolic_math")

            compact = _strip_fences(query).strip()
            if "\n" in compact or len(compact) > 240:
                return None
            if "=" in compact:
                left, right = compact.split("=", 1)
                expr = parse_expr(left, transformations=transformations) - parse_expr(right, transformations=transformations)
                symbols = sorted(expr.free_symbols, key=lambda s: s.name)
                value = sp.solve(expr, symbols[0]) if symbols else sp.solve(expr)
            else:
                expr = parse_expr(compact, transformations=transformations)
                value = sp.simplify(expr)
            return PrimitiveResult(str(value), backend="symbolic_math")
        except Exception:
            return None

    def _extract_field(self, query: str, question: str) -> PrimitiveResult | None:
        field_names = _requested_fields(query)
        if not field_names:
            return None
        payload = _first_json(query) or _first_json(question)
        if isinstance(payload, Mapping):
            extracted = {}
            for field in field_names:
                found = _lookup_path(payload, field)
                if found is not None:
                    extracted[field] = found
            if extracted:
                return PrimitiveResult(
                    json.dumps(extracted, ensure_ascii=False, sort_keys=True),
                    backend="extract_field",
                )
        return None

    def _parse_structured(self, query: str, question: str) -> PrimitiveResult | None:
        payload = _first_json(query) or _first_json(question)
        if payload is not None:
            return PrimitiveResult(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                backend="parse_structured",
            )

        table = _parse_csv_like(_strip_fences(query))
        if table is not None:
            return PrimitiveResult(json.dumps(table, ensure_ascii=False), backend="parse_structured")
        return None

    def _database_query(self, query: str) -> PrimitiveResult | None:
        db_path = os.environ.get("UNO_SQLITE_DATABASE")
        if not db_path:
            if self.strict_optional_backends:
                return PrimitiveResult(
                    "<error reason=\"primitive_backend_not_configured\" primitive=\"database_query\"/>",
                    backend="database_query",
                )
            return None
        sql = _extract_fenced_block(query, ("sql",)) or query.strip()
        if not re.match(r"^\s*(select|with|pragma)\b", sql, re.IGNORECASE):
            return PrimitiveResult("<error reason=\"only_readonly_sql_allowed\"/>", backend="database_query")
        conn = None
        try:
            uri = f"file:{urllib.parse.quote(os.path.abspath(db_path))}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=3.0)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql).fetchmany(int(os.environ.get("UNO_DATABASE_MAX_ROWS", "20")))
            payload = [dict(row) for row in rows]
            return PrimitiveResult(json.dumps(payload, ensure_ascii=False), backend="database_query")
        except Exception as exc:
            return PrimitiveResult(
                f"<error reason=\"database_query_failed\">{_xml_text(str(exc))}</error>",
                backend="database_query",
            )
        finally:
            if conn is not None:
                conn.close()

    def _call_api(self, query: str) -> PrimitiveResult | None:
        if os.environ.get("UNO_ENABLE_CALL_API", "0") != "1":
            if self.strict_optional_backends:
                return PrimitiveResult(
                    "<error reason=\"primitive_backend_not_configured\" primitive=\"call_api\"/>",
                    backend="call_api",
                )
            return None
        payload = _first_json(query)
        if not isinstance(payload, Mapping):
            return PrimitiveResult("<error reason=\"api_schema_not_found\"/>", backend="call_api")
        url = str(payload.get("url", ""))
        if not url.startswith("https://"):
            return PrimitiveResult("<error reason=\"only_https_api_allowed\"/>", backend="call_api")
        method = str(payload.get("method", "GET")).upper()
        if method not in {"GET", "POST"}:
            return PrimitiveResult("<error reason=\"unsupported_api_method\"/>", backend="call_api")
        params = payload.get("params") if isinstance(payload.get("params"), Mapping) else {}
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        body = None
        headers = {"User-Agent": "uno-routing-primitive/1.0"}
        if isinstance(payload.get("headers"), Mapping):
            headers.update({str(k): str(v) for k, v in payload["headers"].items()})
        if method == "POST":
            body = json.dumps(payload.get("json", {})).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            timeout = float(os.environ.get("UNO_CALL_API_TIMEOUT_SEC", "5"))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read(self.output_limit_chars + 1).decode("utf-8", errors="replace")
            return PrimitiveResult(self._truncate(text), backend="call_api")
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            return PrimitiveResult(f"<error reason=\"api_call_failed\">{_xml_text(str(exc))}</error>", backend="call_api")

    def _truncate(self, text: Any) -> str:
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        value = str(text)
        if len(value) <= self.output_limit_chars:
            return value
        return value[: self.output_limit_chars] + "\n<truncated/>"


def _extract_fenced_block(text: str, languages: tuple[str, ...]) -> str | None:
    lang_pat = "|".join(re.escape(lang) for lang in languages)
    match = re.search(rf"```(?:{lang_pat})?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else None


def _strip_fences(text: str) -> str:
    match = re.search(r"```[A-Za-z0-9_-]*\s*\n(.*?)```", text, re.DOTALL)
    return match.group(1).strip() if match else text


def _first_json(text: str) -> Any | None:
    text = _strip_fences(text)
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
            return value
        except json.JSONDecodeError:
            continue
    return None


def _requested_fields(query: str) -> list[str]:
    payload = _first_json(query)
    fields: Any = payload.get("fields") if isinstance(payload, Mapping) else None
    if isinstance(fields, str):
        return [fields]
    if isinstance(fields, list):
        return [str(field) for field in fields if str(field).strip()]

    match = re.search(r"(?:field|fields|keys?)\s*[:=]\s*([A-Za-z0-9_., \-/]+)", query, re.IGNORECASE)
    if not match:
        return []
    return [field.strip() for field in re.split(r"[, ]+", match.group(1)) if field.strip()]


def _lookup_path(payload: Mapping[str, Any], path: str) -> Any | None:
    current: Any = payload
    for part in re.split(r"[./]", path):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return None
    return current


def _parse_csv_like(text: str) -> list[dict[str, str]] | None:
    sample = text.strip()
    if not sample or "\n" not in sample:
        return None
    try:
        dialect = csv.Sniffer().sniff(sample[:1024])
        reader = csv.DictReader(io.StringIO(sample), dialect=dialect)
        rows = [dict(row) for row in reader]
        return rows or None
    except Exception:
        return None


def _xml_text(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
