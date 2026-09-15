import os
from autogen_ext.models.openai import OpenAIChatCompletionClient


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _request_timeout() -> float:
    """Bound provider calls so transport stalls can enter recovery logic."""
    try:
        return max(10.0, float(os.getenv("MODEL_CALL_TIMEOUT_SECONDS", "240")))
    except ValueError:
        return 240.0


def _create_tool_model_client(prefix: str, default_model: str):
    """Create a tool-calling client without granting it workflow authority."""

    api_key = (
        os.getenv(f"{prefix}_API_KEY")
        or os.getenv("FILE_SURFER_API_KEY")
        or os.getenv("MODEL_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    if not api_key:
        raise ValueError(
            f"{prefix} 需要 {prefix}_API_KEY、FILE_SURFER_API_KEY、"
            "MODEL_API_KEY 或 OPENAI_API_KEY"
        )
    return OpenAIChatCompletionClient(
        model=(
            os.getenv(f"{prefix}_MODEL")
            or os.getenv("FILE_SURFER_MODEL")
            or os.getenv("MODEL_NAME")
            or default_model
        ),
        api_key=api_key,
        base_url=(
            os.getenv(f"{prefix}_BASE_URL")
            or os.getenv("FILE_SURFER_BASE_URL")
            or os.getenv("MODEL_BASE_URL")
        ),
        timeout=_request_timeout(),
        model_info={
            "vision": _as_bool(os.getenv(f"{prefix}_VISION"), False),
            "function_calling": True,
            "json_output": True,
            "structured_output": False,
            "family": "unknown",
        },
    )



def create_file_surfer_model_client():
    """
    专门给 FileSurfer 使用的模型。
    这个模型必须真实支持 function calling / tool calling。
    """

    return _create_tool_model_client("FILE_SURFER", "qwen-plus")


def create_web_surfer_model_client():
    """Dedicated browser client; text-only tool models are supported."""

    return _create_tool_model_client("WEB_SURFER", "qwen-plus")

def create_model_client():
    """
    支持 OpenAI 官方接口，也支持 DeepSeek、Qwen、SiliconFlow 等 OpenAI 兼容接口。

    需要设置：
    export MODEL_API_KEY="你的key"
    export MODEL_BASE_URL="https://api.deepseek.com"
    export MODEL_NAME="deepseek-chat"
    """

    model_name = os.getenv("MODEL_NAME", "deepseek-chat")
    api_key = os.getenv("MODEL_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("MODEL_BASE_URL")

    if not api_key:
        raise ValueError("请先设置 MODEL_API_KEY 或 OPENAI_API_KEY")

    # 如果设置了 MODEL_BASE_URL，说明用的是三方 OpenAI 兼容接口
    if base_url:
        return OpenAIChatCompletionClient(
            model=model_name,
            api_key=api_key,
            base_url=base_url,
            timeout=_request_timeout(),
            model_info={
                "vision": False,
                "function_calling": False,
                "json_output": False,
                "structured_output": False,
                "family": "unknown",
            },
        )

    # 如果没设置 base_url，就走 OpenAI 官方
    return OpenAIChatCompletionClient(
        model=model_name,
        api_key=api_key,
        timeout=_request_timeout(),
    )


def model_route_config() -> dict[str, dict[str, str]]:
    """Return env-configured logical device/edge/cloud model routes.

    All routes may share one DashScope-compatible endpoint.  The API key is
    deliberately not returned so this structure is safe for run evidence and
    dashboard diagnostics.
    """
    base_url = os.getenv("MODEL_BASE_URL", "")
    return {
        "device": {
            "model": os.getenv("DEVICE_MODEL", "qwen3.5-flash"),
            "base_url": os.getenv("DEVICE_BASE_URL", base_url),
        },
        "edge": {
            "model": os.getenv("EDGE_MODEL", os.getenv("DEVICE_MODEL", "qwen3.5-flash")),
            "base_url": os.getenv("EDGE_BASE_URL", base_url),
        },
        "cloud": {
            "model": os.getenv("CLOUD_MODEL", os.getenv("MODEL_NAME", "qwen3.5-35b-a3b")),
            "base_url": os.getenv("CLOUD_BASE_URL", base_url),
        },
    }
