from __future__ import annotations

import asyncio
import base64
import re
from typing import Any

import httpx
from fastapi import HTTPException
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from .agent.builder import _build_byok_chat_model
from .config import Settings
from .llm.ledger import _provider_tools, _rewrite_tool_names
from .model_profiles import default_model_protocol
from .model_services import openai_compatible_endpoint


async def _verify_model_service_compatibility(
    *,
    settings: Settings,
    base_url: str,
    adapter_id: str,
    protocol: str | None = None,
    api_key: str,
    model_id: str,
) -> None:
    # ponytail: this probe needs one tool call and one short acknowledgement.
    probe_max_tokens = 512
    probe_timeout_seconds = min(30.0, settings.model_request_timeout_seconds)
    success_signal = "SHEJANE_MODEL_TOOL_LOOP_OK"
    ping = StructuredTool.from_function(
        lambda: success_signal,
        name="shejane.ping",
        description="Return a compatibility signal.",
    )
    try:
        async with asyncio.timeout(probe_timeout_seconds):
            model = _build_byok_chat_model(
                settings=settings,
                model_binding={
                    "adapter_id": adapter_id,
                    "protocol": protocol or default_model_protocol(adapter_id, "agent_chat"),
                    "base_url": base_url,
                    "model_id": model_id,
                    "profile": {
                        "tool_calling": True,
                        "image_inputs": False,
                        "max_output_tokens": probe_max_tokens,
                    },
                },
                model_api_key=api_key,
            ).bind(max_tokens=probe_max_tokens)
            prompt = HumanMessage(
                content=(
                    "Call the ping tool exactly once. After receiving its result, "
                    f"answer exactly {success_signal}."
                )
            )
            provider_tools, aliases, choices = _provider_tools([ping])
            bound = model.bind_tools(provider_tools)
            tool_request = _rewrite_tool_names(await bound.ainvoke([prompt]), aliases)
            calls = list(getattr(tool_request, "tool_calls", ()) or ())
            if len(calls) != 1 or calls[0].get("name") != "shejane.ping" or not calls[0].get("id"):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "incompatible_model",
                        "message": "模型没有完成工具调用，无法用于 Agent 任务。",
                    },
                )
            result = ping.invoke(calls[0].get("args") or {})
            provider_tool_request = _rewrite_tool_names(tool_request, choices)
            provider_result = _rewrite_tool_names(
                ToolMessage(
                    content=result,
                    name="shejane.ping",
                    tool_call_id=str(calls[0]["id"]),
                ),
                choices,
            )
            final = _rewrite_tool_names(
                await bound.ainvoke(
                    [
                        prompt,
                        provider_tool_request,
                        provider_result,
                    ]
                ),
                aliases,
            )
    except HTTPException:
        raise
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code == 401:
            raise HTTPException(
                status_code=401,
                detail={"code": "invalid_api_key", "message": "API Key 无效，请检查后重试。"},
            ) from exc
        if status_code == 403:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "provider_permission_denied",
                    "message": "当前账户或 API Key 没有调用该模型的权限。",
                },
            ) from exc
        if status_code == 402:
            raise HTTPException(
                status_code=402,
                detail={"code": "billing_required", "message": "模型服务账户余额或额度不足。"},
            ) from exc
        if status_code == 429:
            raise HTTPException(
                status_code=429,
                detail={"code": "rate_limited", "message": "模型服务请求过于频繁，请稍后重试。"},
            ) from exc
        if (
            isinstance(exc, TimeoutError)
            or status_code == 408
            or (isinstance(status_code, int) and status_code >= 500)
        ):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "provider_unavailable",
                    "message": "模型服务暂时无法连接，请稍后重试。",
                },
            ) from exc
        upstream = re.sub(r"\s+", " ", str(exc).replace(api_key, "[redacted]")).strip()[:240]
        raise HTTPException(
            status_code=409,
            detail={
                "code": "incompatible_model",
                "message": (
                    "模型服务拒绝了完整工具调用测试，请检查模型与接口格式。"
                    + (f" 服务返回：{upstream}" if upstream else "")
                ),
            },
        ) from exc
    content = final.content
    final_text = (
        content
        if isinstance(content, str)
        else "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
        if isinstance(content, list)
        else ""
    )
    if getattr(final, "tool_calls", None) or final_text.strip() != success_signal:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "incompatible_model",
                "message": "模型没有在工具执行后返回最终答案。",
            },
        )


def _adapter_for_model_protocol(protocol: str) -> str:
    if protocol in {"openai_chat_completions", "openai_responses"}:
        return "openai_chat"
    if protocol == "anthropic_messages":
        return "anthropic_messages"
    if protocol == "google_generate_content":
        return "google_genai"
    raise HTTPException(status_code=422, detail="selected protocol is not a chat protocol")


def _model_probe_failure(exc: Exception, *, api_key: str, message: str) -> HTTPException:
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None:
        status_code = getattr(response, "status_code", None)
    if response is not None:
        try:
            payload = response.json()
        except (ValueError, TypeError):
            payload = None
        provider_error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(provider_error, dict) and provider_error.get("code") == "get_channel_failed":
            upstream = re.sub(
                r"\s+",
                " ",
                str(provider_error.get("message") or "").replace(api_key, "[redacted]"),
            ).strip()[:240]
            return HTTPException(
                status_code=409,
                detail={
                    "code": "model_unavailable",
                    "message": (
                        "当前 API Key 所在分组没有此模型的可用渠道，请更换模型、分组或 API Key。"
                        + (f" 服务返回：{upstream}" if upstream else "")
                    ),
                },
            )
    if status_code == 401:
        return HTTPException(
            status_code=401,
            detail={"code": "invalid_api_key", "message": "API Key 无效，请检查后重试。"},
        )
    if status_code == 403:
        return HTTPException(
            status_code=403,
            detail={
                "code": "provider_permission_denied",
                "message": "当前账户或 API Key 没有调用该模型的权限。",
            },
        )
    if status_code == 402:
        return HTTPException(
            status_code=402,
            detail={"code": "billing_required", "message": "模型服务账户余额或额度不足。"},
        )
    if status_code == 429:
        return HTTPException(
            status_code=429,
            detail={"code": "rate_limited", "message": "模型服务请求过于频繁，请稍后重试。"},
        )
    if (
        isinstance(exc, httpx.RequestError)
        or status_code == 408
        or (isinstance(status_code, int) and status_code >= 500)
    ):
        return HTTPException(
            status_code=503,
            detail={
                "code": "provider_unavailable",
                "message": "模型服务暂时无法连接，请稍后重试。",
            },
        )
    upstream = re.sub(r"\s+", " ", str(exc).replace(api_key, "[redacted]")).strip()[:240]
    return HTTPException(
        status_code=409,
        detail={
            "code": "incompatible_model",
            "message": message + (f" 服务返回：{upstream}" if upstream else ""),
        },
    )


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


async def _verify_model_image_understanding(
    *,
    settings: Settings,
    base_url: str,
    protocol: str,
    api_key: str,
    model_id: str,
) -> None:
    success_signal = "RED"
    image_base64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEUlEQVR42mP8z4AdMDEMKQkA"
        "zUEBD7t4NqoAAAAASUVORK5CYII="
    )
    image_block: dict[str, Any]
    if protocol == "anthropic_messages":
        image_block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": image_base64,
            },
        }
    else:
        image_block = {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{image_base64}"},
        }
    try:
        model = _build_byok_chat_model(
            settings=settings,
            model_binding={
                "adapter_id": _adapter_for_model_protocol(protocol),
                "protocol": protocol,
                "base_url": base_url,
                "model_id": model_id,
                "profile": {"image_inputs": True, "max_output_tokens": 64},
            },
            model_api_key=api_key,
        ).bind(max_tokens=64)
        response = await model.ainvoke(
            [
                HumanMessage(
                    content=[
                        {
                            "type": "text",
                            "text": (
                                "What is the dominant color in this image? "
                                "Reply with one uppercase English word only."
                            ),
                        },
                        image_block,
                    ]
                )
            ]
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _model_probe_failure(
            exc,
            api_key=api_key,
            message="模型服务拒绝了图片输入测试，请检查模型与接口格式。",
        ) from exc
    if _message_text(response.content).strip() != success_signal:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "incompatible_model",
                "message": "模型没有正确响应图片输入测试。",
            },
        )


async def _verify_model_image_generation(
    *,
    settings: Settings,
    base_url: str,
    api_key: str,
    model_id: str,
) -> None:
    try:
        async with httpx.AsyncClient(
            timeout=settings.model_request_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                openai_compatible_endpoint(base_url, "images/generations"),
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model_id,
                    "prompt": "A plain white square on a plain white background.",
                    "n": 1,
                },
            )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise _model_probe_failure(
            exc,
            api_key=api_key,
            message="模型服务拒绝了图片生成测试，请检查模型与接口格式。",
        ) from exc
    raw_results = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(raw_results, dict):
        raw_results = raw_results.get("images", [raw_results])
    if raw_results is None and isinstance(payload, dict):
        raw_results = payload.get("images")
    valid_result = isinstance(raw_results, list) and any(
        isinstance(item, dict)
        and bool(item.get("url") or item.get("b64_json") or item.get("base64"))
        for item in raw_results
    )
    if not valid_result:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "incompatible_model",
                "message": "图片生成接口没有返回可用的图片结果。",
            },
        )


async def _verify_model_image_editing(
    *,
    settings: Settings,
    base_url: str,
    api_key: str,
    model_id: str,
) -> None:
    test_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEUlEQVR42mP8z4AdMDEMKQkA"
        "zUEBD7t4NqoAAAAASUVORK5CYII="
    )
    try:
        async with httpx.AsyncClient(
            timeout=settings.model_request_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                openai_compatible_endpoint(base_url, "images/edits"),
                headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
                data={
                    "model": model_id,
                    "prompt": "Keep this plain red square unchanged.",
                    "n": "1",
                },
                files={"image": ("probe.png", test_png, "image/png")},
            )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise _model_probe_failure(
            exc,
            api_key=api_key,
            message="模型服务拒绝了图片编辑测试，请检查模型与接口格式。",
        ) from exc
    raw_results = payload.get("data") if isinstance(payload, dict) else None
    if not (
        isinstance(raw_results, list)
        and any(
            isinstance(item, dict)
            and bool(item.get("url") or item.get("b64_json") or item.get("base64"))
            for item in raw_results
        )
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "incompatible_model",
                "message": "图片编辑接口没有返回可用的图片结果。",
            },
        )


async def _verify_model_service_capability(
    *,
    settings: Settings,
    base_url: str,
    capability: str,
    protocol: str,
    api_key: str,
    model_id: str,
) -> None:
    if capability == "agent_chat":
        await _verify_model_service_compatibility(
            settings=settings,
            base_url=base_url,
            adapter_id=_adapter_for_model_protocol(protocol),
            protocol=protocol,
            api_key=api_key,
            model_id=model_id,
        )
        return
    if capability == "image_understanding":
        await _verify_model_image_understanding(
            settings=settings,
            base_url=base_url,
            protocol=protocol,
            api_key=api_key,
            model_id=model_id,
        )
        return
    if capability == "image_generation":
        await _verify_model_image_generation(
            settings=settings,
            base_url=base_url,
            api_key=api_key,
            model_id=model_id,
        )
        return
    await _verify_model_image_editing(
        settings=settings,
        base_url=base_url,
        api_key=api_key,
        model_id=model_id,
    )
