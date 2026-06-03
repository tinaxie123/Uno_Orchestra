"""Closed routing primitive vocabulary for UNO schema v1.1."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PrimitiveSpec:
    id: str
    cluster: str
    contract: str
    prompt: str
    backend: str


@dataclass(frozen=True)
class Route:
    round: int
    subtask: int
    model: str
    skill: str
    query: str


@dataclass(frozen=True)
class PrimitiveResult:
    text: str
    output_tokens: int = 0
    billable: bool = False
    backend: str = "local"
    timed_out: bool = False


PRIMITIVES: dict[str, PrimitiveSpec] = {
    "direct_answer": PrimitiveSpec(
        id="direct_answer",
        cluster="answer_reason",
        contract="Solve the subtask directly without invoking tools or further routing.",
        prompt="Answer the following subtask directly and concisely.",
        backend="langchain_subagent",
    ),
    "reason": PrimitiveSpec(
        id="reason",
        cluster="answer_reason",
        contract="Produce explicit reasoning before committing to an answer.",
        prompt="Give a concise rationale for the following subtask, then provide the answer.",
        backend="langchain_subagent",
    ),
    "web_search": PrimitiveSpec(
        id="web_search",
        cluster="retrieve",
        contract="Issue search-engine queries and return ranked snippets with provenance URLs.",
        prompt=(
            "Answer as a search retriever. Return concise ranked snippets and include provenance URLs "
            "when available. If provenance is unavailable, say so."
        ),
        backend="mcp_or_langchain_subagent",
    ),
    "database_query": PrimitiveSpec(
        id="database_query",
        cluster="retrieve",
        contract="Execute a structured query against a tabular or graph knowledge base.",
        prompt="Answer as a database query engine. Return only the queried records or a compact summary.",
        backend="local_or_langchain_subagent",
    ),
    "fact_check": PrimitiveSpec(
        id="fact_check",
        cluster="retrieve",
        contract="Verify a single factual claim against an authoritative source and return a verdict.",
        prompt=(
            "Fact-check the claim. Return verdict, short evidence, and authoritative source URLs "
            "when available."
        ),
        backend="mcp_or_langchain_subagent",
    ),
    "read_document": PrimitiveSpec(
        id="read_document",
        cluster="skills",
        contract="Read a long document and return targeted excerpts or a faithful summary.",
        prompt="Read the provided document or passage and answer with targeted excerpts or a faithful summary.",
        backend="langchain_subagent",
    ),
    "read_code": PrimitiveSpec(
        id="read_code",
        cluster="skills",
        contract="Parse a source-code file and reason about its behavior or structure.",
        prompt="Analyze the provided source code and answer about its behavior or structure.",
        backend="langchain_subagent",
    ),
    "extract_field": PrimitiveSpec(
        id="extract_field",
        cluster="skills",
        contract="Return one or more named fields from a structured input payload.",
        prompt="Extract the requested field or fields from the provided input. Return only the extracted values.",
        backend="local_or_langchain_subagent",
    ),
    "parse_structured": PrimitiveSpec(
        id="parse_structured",
        cluster="skills",
        contract="Convert free-form text into a typed object such as JSON or a record.",
        prompt="Parse the input into the requested structured representation. Prefer valid JSON when possible.",
        backend="local_or_langchain_subagent",
    ),
    "execute_python": PrimitiveSpec(
        id="execute_python",
        cluster="execute",
        contract="Emit a self-contained Python program; the sandbox returns stdout and stderr.",
        prompt="Execute the supplied self-contained Python program and return stdout and stderr.",
        backend="local",
    ),
    "execute_shell": PrimitiveSpec(
        id="execute_shell",
        cluster="execute",
        contract="Emit a single shell command; the sandbox returns captured terminal output.",
        prompt="Execute the supplied shell command and return stdout and stderr.",
        backend="local",
    ),
    "call_api": PrimitiveSpec(
        id="call_api",
        cluster="execute",
        contract="Issue a typed function call to an external HTTPS service per a declared schema.",
        prompt="Call the requested API according to the declared schema and return the response compactly.",
        backend="local_or_mcp",
    ),
    "symbolic_math": PrimitiveSpec(
        id="symbolic_math",
        cluster="symbolic",
        contract="Invoke a computer-algebra backend for exact algebraic manipulation.",
        prompt="Solve the symbolic math request exactly. Return the exact result with minimal explanation.",
        backend="local_or_langchain_subagent",
    ),
}

VALID_PRIMITIVES = frozenset(PRIMITIVES)
PRIMITIVE_PROMPTS = {name: spec.prompt for name, spec in PRIMITIVES.items()}
