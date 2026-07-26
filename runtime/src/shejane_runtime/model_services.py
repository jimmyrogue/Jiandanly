"""Runtime-owned model-service presets and compatibility choices."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

ModelAdapterID = Literal["openai_chat", "anthropic_messages"]

_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "description": "推理和通用任务，按 DeepSeek 官方价格计费。",
        "api_key_url": "https://platform.deepseek.com/api_keys",
        "billing_url": "https://platform.deepseek.com/usage",
        "adapter_id": "openai_chat",
        "regions": (
            {
                "id": "cn",
                "name": "中国站",
                "default": True,
                "base_url": "https://api.deepseek.com/v1",
            },
        ),
        "models": (
            {
                "model_id": "deepseek-v4-flash",
                "display_name": "DeepSeek V4 Flash",
                "source": "bundled",
                "verification": "verified",
                "recommended": True,
                "tool_calling": True,
                "streaming": True,
                "image_inputs": False,
            },
            {
                "model_id": "deepseek-v4-pro",
                "display_name": "DeepSeek V4 Pro",
                "source": "bundled",
                "verification": "verified",
                "recommended": False,
                "tool_calling": True,
                "streaming": True,
                "image_inputs": False,
            },
        ),
    },
    {
        "id": "kimi",
        "name": "Kimi",
        "description": "适合长上下文、代码和复杂任务。",
        "api_key_url": "https://platform.moonshot.cn/console/api-keys",
        "billing_url": "https://platform.moonshot.cn/console/billing",
        "adapter_id": "openai_chat",
        "regions": (
            {
                "id": "cn",
                "name": "中国站",
                "default": True,
                "base_url": "https://api.moonshot.cn/v1",
            },
            {
                "id": "intl",
                "name": "国际站",
                "default": False,
                "base_url": "https://api.moonshot.ai/v1",
            },
        ),
        "models": (
            {
                "model_id": "kimi-k2.6",
                "display_name": "Kimi K2.6",
                "source": "bundled",
                "verification": "verified",
                "recommended": True,
                "tool_calling": True,
                "streaming": True,
                "image_inputs": True,
            },
        ),
    },
    {
        "id": "qwen",
        "name": "千问",
        "description": "阿里云百炼提供的通义千问模型。",
        "api_key_url": "https://bailian.console.aliyun.com/?tab=model#/api-key",
        "billing_url": "https://usercenter2.aliyun.com/home",
        "adapter_id": "openai_chat",
        "regions": (
            {
                "id": "cn",
                "name": "中国站",
                "default": True,
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            },
            {
                "id": "intl",
                "name": "国际站",
                "default": False,
                "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            },
        ),
        "models": (
            {
                "model_id": "qwen3.7-plus",
                "display_name": "Qwen 3.7 Plus",
                "source": "bundled",
                "verification": "verified",
                "recommended": True,
                "tool_calling": True,
                "streaming": True,
                "image_inputs": True,
            },
        ),
    },
    {
        "id": "glm",
        "name": "GLM",
        "description": "智谱提供的中文、推理和 Agent 模型。",
        "api_key_url": "https://open.bigmodel.cn/usercenter/apikeys",
        "billing_url": "https://open.bigmodel.cn/usercenter/finance",
        "adapter_id": "openai_chat",
        "regions": (
            {
                "id": "cn",
                "name": "中国站",
                "default": True,
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
            },
            {
                "id": "intl",
                "name": "国际站",
                "default": False,
                "base_url": "https://api.z.ai/api/paas/v4",
            },
        ),
        "models": (
            {
                "model_id": "glm-5",
                "display_name": "GLM-5",
                "source": "bundled",
                "verification": "verified",
                "recommended": True,
                "tool_calling": True,
                "streaming": True,
                "image_inputs": False,
            },
        ),
    },
    {
        "id": "minimax",
        "name": "MiniMax",
        "description": "适合代码、长上下文和 Agent 工作流。",
        "api_key_url": "https://platform.minimaxi.com/user-center/basic-information/interface-key",
        "billing_url": "https://platform.minimaxi.com/user-center/payment",
        "adapter_id": "openai_chat",
        "regions": (
            {
                "id": "cn",
                "name": "中国站",
                "default": True,
                "base_url": "https://api.minimaxi.com/v1",
            },
            {
                "id": "intl",
                "name": "国际站",
                "default": False,
                "base_url": "https://api.minimax.io/v1",
            },
        ),
        "models": (
            {
                "model_id": "MiniMax-M2.7",
                "display_name": "MiniMax M2.7",
                "source": "bundled",
                "verification": "verified",
                "recommended": True,
                "tool_calling": True,
                "streaming": True,
                "image_inputs": False,
            },
        ),
    },
    {
        "id": "siliconflow",
        "name": "硅基流动",
        "description": "通过一个账户使用多家厂商的模型。",
        "api_key_url": "https://cloud.siliconflow.cn/account/ak",
        "billing_url": "https://cloud.siliconflow.cn/account/recharge",
        "adapter_id": "openai_chat",
        "regions": (
            {
                "id": "cn",
                "name": "中国站",
                "default": True,
                "base_url": "https://api.siliconflow.cn/v1",
            },
            {
                "id": "intl",
                "name": "国际站",
                "default": False,
                "base_url": "https://api.siliconflow.com/v1",
            },
        ),
        "models": (
            {
                "model_id": "Pro/zai-org/GLM-5",
                "display_name": "GLM-5 Pro",
                "source": "bundled",
                "verification": "verified",
                "recommended": True,
                "tool_calling": True,
                "streaming": True,
                "image_inputs": False,
            },
        ),
    },
    {
        "id": "custom",
        "name": "连接已有服务",
        "description": "连接中转站、AI Gateway 或其他兼容服务。",
        "api_key_url": None,
        "billing_url": None,
        "adapter_id": None,
        "regions": (),
        "models": (),
    },
)


def list_model_service_presets() -> list[dict[str, Any]]:
    """Return the user-facing preset catalog with editable service addresses."""
    return [
        {
            "id": preset["id"],
            "name": preset["name"],
            "description": preset["description"],
            "api_key_url": preset["api_key_url"],
            "billing_url": preset["billing_url"],
            "regions": [
                {
                    "id": region["id"],
                    "name": region["name"],
                    "default": region["default"],
                    "base_url": region["base_url"],
                }
                for region in preset["regions"]
            ],
        }
        for preset in _PRESETS
    ]


def model_service_preset(preset_id: str) -> dict[str, Any] | None:
    """Return one private Runtime preset, including fixed transport details."""
    for preset in _PRESETS:
        if preset["id"] == preset_id:
            return deepcopy(preset)
    return None


def adapter_for_custom_service(
    *,
    openai_chat_available: bool,
    anthropic_messages_available: bool,
) -> ModelAdapterID | None:
    """Choose a detected custom-service adapter without asking ordinary users."""
    if openai_chat_available:
        return "openai_chat"
    if anthropic_messages_available:
        return "anthropic_messages"
    return None
