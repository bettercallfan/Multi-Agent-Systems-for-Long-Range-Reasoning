"""Probe configured DashScope/OpenAI-compatible model routes without printing secrets."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from openai import OpenAI

from config import model_route_config


def probe(route: str, model: str, base_url: str) -> dict:
    started = time.perf_counter()
    try:
        client = OpenAI(
            api_key=os.environ.get("MODEL_API_KEY") or os.environ.get("OPENAI_API_KEY"),
            base_url=base_url or None,
            timeout=60,
        )
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "只回复 OK"}],
            temperature=0,
            max_tokens=8,
        )
        usage = response.usage
        return {
            "route": route,
            "configured_model": model,
            "returned_model": getattr(response, "model", None),
            "status": "passed",
            "latency_seconds": round(time.perf_counter() - started, 3),
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        }
    except Exception as exc:
        return {
            "route": route,
            "configured_model": model,
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:300],
            "latency_seconds": round(time.perf_counter() - started, 3),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", choices=("device", "edge", "cloud"), action="append")
    args = parser.parse_args()
    if not os.environ.get("MODEL_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
        parser.error("缺少 MODEL_API_KEY 或 OPENAI_API_KEY；密钥不会被打印")
    routes = model_route_config()
    selected = args.route or ["device", "edge", "cloud"]
    results = [probe(route, routes[route]["model"], routes[route]["base_url"]) for route in selected]
    print(json.dumps({"results": results, "all_passed": all(item["status"] == "passed" for item in results)},
                     ensure_ascii=False, indent=2))
    return 0 if all(item["status"] == "passed" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
