#!/usr/bin/env python3
"""Preflight checks for reproducible evaluation runs."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _ok(msg: str) -> None:
    print(f"[OK]   {msg}")


def _warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _check_module(name: str, package_hint: str | None = None, required: bool = True) -> bool:
    if _has_module(name):
        _ok(f"Python module importable: {name}")
        return True
    hint = f" Install with `{package_hint}`." if package_hint else ""
    (_fail if required else _warn)(f"Python module missing: {name}.{hint}")
    return False


def _check_openai_endpoint(label: str, base_url: str | None, api_key: str = "EMPTY", required: bool = True) -> bool:
    if not base_url:
        (_fail if required else _warn)(f"{label} is not set")
        return False
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = resp.read(2048).decode("utf-8", errors="replace")
        try:
            data = json.loads(payload)
            count = len(data.get("data", [])) if isinstance(data, dict) else "unknown"
        except json.JSONDecodeError:
            count = "unknown"
        _ok(f"{label} reachable at {url} (models={count})")
        return True
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        (_fail if required else _warn)(f"{label} not reachable at {url}: {exc}")
        return False


def _check_docker(required: bool = False) -> bool:
    if not shutil.which("docker"):
        (_fail if required else _warn)("Docker CLI not found")
        return False
    try:
        proc = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                              capture_output=True, text=True, timeout=5)
    except Exception as exc:
        (_fail if required else _warn)(f"Docker check failed: {exc}")
        return False
    if proc.returncode != 0:
        (_fail if required else _warn)(f"Docker daemon not available: {proc.stderr.strip()}")
        return False
    _ok(f"Docker daemon available ({proc.stdout.strip()})")

    compose = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, timeout=5)
    if compose.returncode == 0:
        _ok(compose.stdout.strip())
        return True
    (_fail if required else _warn)("`docker compose` is not available")
    return False


def _check_terminal_bench(required: bool = False) -> bool:
    default_path = Path(__file__).resolve().parents[1] / "data" / "terminal-bench" / "tasks"
    path = Path(os.environ.get("TERMINAL_BENCH_TASKS_DIR", str(default_path)))
    if not path.exists():
        (_fail if required else _warn)(
            f"Terminal-Bench task directory not found: {path}. Set TERMINAL_BENCH_TASKS_DIR."
        )
        return False
    task_tomls = list(path.glob("*/*/task.toml"))
    if not task_tomls:
        (_fail if required else _warn)(f"No task.toml files found under Terminal-Bench directory: {path}")
        return False
    _ok(f"Terminal-Bench tasks found: {len(task_tomls)} under {path}")
    return True


def _check_swebench(required: bool = False) -> bool:
    has_pkg = _check_module("swebench", "pip install -e '.[docker]'", required=False)
    if not has_pkg:
        (_fail if required else _warn)("SWE-bench official harness is optional but required for one-shot official verification")
    return has_pkg


def _check_dataset_access(dataset: str, split: str, required: bool = False) -> bool:
    if not _has_module("datasets"):
        (_fail if required else _warn)("Cannot check datasets because `datasets` is missing")
        return False
    try:
        from datasets import load_dataset
        ds = load_dataset(dataset, split=f"{split}[:1]", trust_remote_code=True)
        _ok(f"Hugging Face dataset accessible: {dataset} ({len(ds)} sample)")
        return True
    except Exception as exc:
        (_fail if required else _warn)(f"Dataset check failed for {dataset}: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env", help="Optional dotenv file to load")
    parser.add_argument("--skip-endpoints", action="store_true", help="Do not check LOCAL_BASE/API_BASE")
    parser.add_argument("--docker", action="store_true", help="Require Docker/SWE/Terminal-Bench checks")
    parser.add_argument("--datasets", action="store_true", help="Check representative Hugging Face datasets")
    args = parser.parse_args()

    _load_dotenv(Path(args.env_file))

    failures = 0
    required_modules = [
        ("yaml", "pip install -e '.[eval]'"),
        ("openai", "pip install -e '.[eval]'"),
        ("datasets", "pip install -e '.[eval]'"),
        ("pydantic", "pip install -e '.[eval]'"),
        ("tqdm", "pip install -e '.[eval]'"),
        ("numpy", "pip install -e '.[eval]'"),
    ]
    for module, hint in required_modules:
        failures += 0 if _check_module(module, hint, required=True) else 1

    if not args.skip_endpoints:
        failures += 0 if _check_openai_endpoint(
            "LOCAL_BASE", os.environ.get("LOCAL_BASE", "http://localhost:8000/v1"), "EMPTY", required=True
        ) else 1
        failures += 0 if _check_openai_endpoint(
            "API_BASE", os.environ.get("API_BASE", "http://localhost:9000/v1"), os.environ.get("API_KEY", "EMPTY"),
            required=True,
        ) else 1

    if args.datasets:
        _check_dataset_access("Idavidrein/gpqa", "train", required=False)
        _check_dataset_access("HuggingFaceH4/MATH-500", "test", required=False)
        _check_dataset_access("openai/openai_humaneval", "test", required=False)

    if args.docker:
        failures += 0 if _check_docker(required=True) else 1
        _check_swebench(required=False)
        failures += 0 if _check_terminal_bench(required=True) else 1
    else:
        _check_docker(required=False)
        _check_terminal_bench(required=False)

    if failures:
        print(f"\nPreflight finished with {failures} required failure(s).")
        return 1
    print("\nPreflight passed for the selected checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
