import os
from autogen_ext.models.openai import OpenAIChatCompletionClient



def create_file_surfer_model_client():
    """
    专门给 FileSurfer 使用的模型。
    这个模型必须真实支持 function calling / tool calling。
    """

    return OpenAIChatCompletionClient(
        model=os.getenv("FILE_SURFER_MODEL", "qwen-plus"),
        api_key=os.getenv("FILE_SURFER_API_KEY"),
        base_url=os.getenv("FILE_SURFER_BASE_URL", None),
        model_info={
            "vision": False,
            "function_calling": True,
            "json_output": True,
            "structured_output": False,
            "family": "unknown",
        },
    )

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
    )
