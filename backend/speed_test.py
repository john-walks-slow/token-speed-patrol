import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import httpx

from .rate_limit import limiter
from .url_utils import normalize_base_url

# core 层不依赖桌面版的 SQLite 设置；网络参数由调用方注入（如桌面版传
# network_settings.client_kwargs()），不传时用默认：跟随环境代理、系统 TLS 校验
DEFAULT_CLIENT_KWARGS = {"trust_env": True}


def _reconcile_token_counts(completion_tokens: int, reasoning_tokens: int) -> tuple[int, int, int]:
    """统一两种 usage 口径，返回 (tokens_generated, reasoning_tokens, content_tokens)。

    - OpenAI/DeepSeek 口径：completion_tokens 已含 reasoning → content = completion - reasoning
    - Gemini/部分网关转发口径（如 CLIProxyAPI 转发 Gemini）：completion_tokens 仅含正文，
      reasoning_tokens 独立上报 → content = completion，generated = completion + reasoning

    判据：reasoning > completion 时只可能是后者（content 不可能为负）。
    """
    if reasoning_tokens > completion_tokens:
        return completion_tokens + reasoning_tokens, reasoning_tokens, completion_tokens
    return completion_tokens, reasoning_tokens, max(completion_tokens - reasoning_tokens, 0)


def _extract_reasoning_tokens(usage: dict) -> int:
    """从 usage 中读取 reasoning/thinking token 数，兼容多厂商字段。

    OpenAI/DeepSeek: completion_tokens_details.reasoning_tokens（计入 completion_tokens）
    OpenAI Responses: output_tokens_details.reasoning_tokens
    Anthropic 兼容层: output_tokens_details.thinking_tokens
    xAI(gRPC): 顶层 reasoning_tokens
    Gemini: thoughts_token_count 为独立字段、不计入 candidates_token_count，
    content_tokens = completion - reasoning 天然正确，无需在此读取。
    解析失败/缺失统一返回 0，由调用方以 content = completion - reasoning 兜底。
    """
    try:
        if not isinstance(usage, dict):
            return 0
        for wrapper in ("completion_tokens_details", "output_tokens_details"):
            details = usage.get(wrapper) or {}
            if not isinstance(details, dict):
                continue
            for key in ("reasoning_tokens", "thinking_tokens"):
                val = details.get(key)
                if isinstance(val, int) and val > 0:
                    return val
        val = usage.get("reasoning_tokens")
        if isinstance(val, int) and val > 0:
            return val
        return 0
    except Exception:
        return 0


def _extract_input_tokens(usage: dict, protocol: str) -> int:
    """从 usage 读取输入 token 数。OpenAI: prompt_tokens；Anthropic: input_tokens。"""
    if not isinstance(usage, dict):
        return 0
    key = "input_tokens" if protocol == "anthropic" else "prompt_tokens"
    val = usage.get(key)
    return val if isinstance(val, int) and val > 0 else 0


async def _raise_for_openai_error(resp: httpx.Response) -> None:
    """OpenAI 兼容端点非 2xx 时，读取响应体 error.message 提升异常信息。

    默认 raise_for_status 只含状态码与 URL，不含具体原因（如 400 Unknown parameter: thinking），
    用户看不到"为什么失败"。此处把响应体里的 error.message 拼进异常文本。
    """
    if resp.status_code < 400:
        return
    detail = ""
    try:
        await resp.aread()
        data = resp.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                detail = err.get("message") or ""
            elif isinstance(err, str):
                detail = err
    except Exception:
        pass
    message = f"HTTP {resp.status_code}"
    if detail:
        message += f": {detail}"
    raise httpx.HTTPStatusError(message, request=resp.request, response=resp)


def _extract_stream_chunk(chunk: dict, protocol: str) -> dict:
    """提取流式 chunk 的统一字段，屏蔽 openai/anthropic 事件结构差异。

    返回 {text, reasoning, model, usage, done}。text/reasoning 为该 chunk 的增量文本，
    model 有值则更新 actual_model，usage 有值则用于更新 token 统计，done 表示流已结束。
    """
    if protocol == "anthropic":
        etype = chunk.get("type")
        if etype == "message_start":
            msg = chunk.get("message") or {}
            return {"text": None, "reasoning": None, "model": msg.get("model"), "usage": None, "done": False}
        if etype == "content_block_delta":
            delta = chunk.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                return {"text": delta.get("text"), "reasoning": None, "model": None, "usage": None, "done": False}
            if dtype == "thinking_delta":
                return {"text": None, "reasoning": delta.get("thinking"), "model": None, "usage": None, "done": False}
            return {"text": None, "reasoning": None, "model": None, "usage": None, "done": False}
        if etype == "message_delta":
            return {"text": None, "reasoning": None, "model": None, "usage": chunk.get("usage"), "done": False}
        if etype == "message_stop":
            return {"text": None, "reasoning": None, "model": None, "usage": None, "done": True}
        return {"text": None, "reasoning": None, "model": None, "usage": None, "done": False}

    # openai 兼容
    choices = chunk.get("choices") or []
    delta = choices[0].get("delta", {}) if choices else {}

    def _as_text(val):
        # 个别网关（如 CF Workers AI 的 qwq）在流尾发 content: true / content: 1 等非字符串哨兵，
        # 直接 join 会崩溃（"expected str instance, bool found"），非字符串一律丢弃
        return val if isinstance(val, str) else None

    return {
        "text": _as_text(delta.get("content")),
        "reasoning": _as_text(delta.get("reasoning_content")) or _as_text(delta.get("reasoning")),
        "model": chunk.get("model"),
        "usage": chunk.get("usage"),
        "done": False,  # [DONE] 哨兵由调用方处理
    }


async def list_models(
    base_url: str,
    api_key: str = "",
    protocol: str = "openai",
    client_kwargs: dict | None = None,
) -> tuple[bool, list[dict] | str]:
    """Fetch available models from an OpenAI-compatible / Anthropic API endpoint.

    base_url 需自带路径（含 /v1 等），不再自动拼接。
    protocol=anthropic 时走 Anthropic 原生鉴权头（x-api-key + anthropic-version）。
    """
    base = normalize_base_url(base_url)
    url = f"{base}/models"
    headers = {}
    if api_key:
        if protocol == "anthropic":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=10.0, **(client_kwargs or DEFAULT_CLIENT_KWARGS)) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            models = [
                {"id": m["id"], "owned_by": m.get("owned_by", "unknown")}
                for m in data.get("data", [])
            ]
            return True, models
    except Exception as e:
        return False, str(e)


def _compute_itl(content_tokens: int, content_ttft_ms: float | None, total_latency_ms: float) -> float | None:
    """ITL（inter-token latency，ms/token）：(总耗时 - content_ttft) / (content_tokens - 1)。
    扣除首字前的一切等待（排队/网络/思考），反映解码阶段的纯发射耗时；
    首个正文 token 在 content_ttft 已到达，剩余 N-1 个 token 分摊剩余时间（vLLM/Anayscale TPOT 口径）。
    仅流式有 content_ttft，非流式为 None。"""
    if (
        content_ttft_ms is not None
        and content_tokens > 1
        and total_latency_ms > content_ttft_ms
    ):
        return (total_latency_ms - content_ttft_ms) / (content_tokens - 1)
    return None


async def run_speed_test(
    base_url: str,
    api_key: str = "",
    model: str = "",
    prompt: str = "Hello, tell me a short story in 3 sentences.",
    max_tokens: int | None = None,
    temperature: float | None = None,
    stream: bool = False,
    provider_id: str = "",
    provider_name: str = "",
    protocol: str = "openai",
    timeout: float | None = None,
    client_kwargs: dict | None = None,
) -> dict:
    """Run a single speed test against an OpenAI-compatible / Anthropic native API.

    base_url 需自带路径（含 /v1 等），不再自动拼接。
    protocol=anthropic 时请求 {base}/messages，用 x-api-key + anthropic-version，
    且不发送 temperature（Anthropic 4.7+ 模型已移除该参数，省略最稳妥）。
    max_tokens 为 None 时不发送该字段，由上游用自身默认上限。
    temperature 为 None 时不发送该字段（部分模型仅允许默认值 1）。
    timeout 为 None 时用默认 120s；巡检慢模型（冷启动推理）可调大。
    """
    base = normalize_base_url(base_url)
    if protocol == "anthropic":
        url = f"{base}/messages"
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if api_key:
            headers["x-api-key"] = api_key
        payload: dict = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": stream,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
    else:
        url = f"{base}/chat/completions"
        headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": stream,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if stream:
            # 多数 OpenAI-compatible 端点默认不返回流式 usage，追加 include_usage 以拿到准确 token 数。
            # 个别不识别该字段的端点会忽略它（OpenAI 兼容约定），不影响请求。
            payload["stream_options"] = {"include_usage": True}

    test_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    ttft_ms = None
    content_ttft_ms = None
    total_latency_ms = 0
    tokens_generated = 0
    reasoning_tokens = 0
    content_tokens = 0
    input_tokens = 0
    actual_model = model
    # 流中是否出现过 reasoning 增量：决定 thinking_ms 是否可测（网关只转正文、
    # 仅在最终 usage 上报思考数时，思考耗时混入 TTFT，无法单独测量）
    reasoning_seen = False
    reasoning_chunk_count = 0
    start = time.perf_counter()

    try:
        if stream:
            content_chunk_count = 0
            first_token = True
            first_content_token = True
            content_parts: list[str] = []

            async with httpx.AsyncClient(timeout=timeout or 120.0, **(client_kwargs or DEFAULT_CLIENT_KWARGS)) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    await _raise_for_openai_error(resp)
                    async for line in resp.aiter_lines():
                        if not line.strip() or line.startswith(":"):
                            continue
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if protocol == "openai" and data_str.strip() == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                                fields = _extract_stream_chunk(chunk, protocol)
                                if fields["done"]:
                                    break
                                if fields["model"]:
                                    actual_model = fields["model"]
                                rc = fields["reasoning"]
                                ct = fields["text"]
                                if rc:
                                    if first_token:
                                        ttft_ms = (time.perf_counter() - start) * 1000
                                        first_token = False
                                    reasoning_seen = True
                                    reasoning_chunk_count += 1
                                if ct:
                                    # ttft = 首个可见 token（reasoning 或正文）；纯正文流时即首个正文
                                    if first_token:
                                        ttft_ms = (time.perf_counter() - start) * 1000
                                        first_token = False
                                    if first_content_token:
                                        content_ttft_ms = (time.perf_counter() - start) * 1000
                                        first_content_token = False
                                    content_chunk_count += 1
                                    content_parts.append(ct)
                                usage = fields["usage"]
                                if usage:
                                    input_tokens = _extract_input_tokens(usage, protocol)
                                    if protocol == "anthropic":
                                        # Anthropic 原生 usage 不拆分思考 token
                                        tokens_generated = usage.get("output_tokens", 0) or 0
                                        reasoning_tokens = 0
                                        content_tokens = tokens_generated
                                    else:
                                        completion = usage.get("completion_tokens", 0) or 0
                                        tokens_generated, reasoning_tokens, content_tokens = _reconcile_token_counts(
                                            completion, _extract_reasoning_tokens(usage)
                                        )
                            except json.JSONDecodeError:
                                continue

            # usage 未在流中返回时会走 chunk 计数回退（近似，understanding 批吐多 token 的端点会低估 token 数）。
            # 新口径下 tokens_generated 参与有效速度，只在确实没有任何 token 产出时置 None。
            if tokens_generated == 0:
                # 以流中实际出现的 reasoning 增量划分：这批 chunk 数即 temp 量级，
                # 若 reasoning 只在 post-usage chunk 中一次性上报而流内无增量，此处 reasoning 为 0，
                # 与 usage 口径的数据可能不一致，但仅作临时回退，正常端点都会返回 usage，不影响常态。
                reasoning_tokens = reasoning_chunk_count
                content_tokens = content_chunk_count
                if reasoning_chunk_count + content_chunk_count > 0:
                    tokens_generated = reasoning_chunk_count + content_chunk_count
                else:
                    tokens_generated = 0

            response_content = "".join(content_parts) or None
            total_latency_ms = (time.perf_counter() - start) * 1000
        else:
            async with httpx.AsyncClient(timeout=timeout or 120.0, **(client_kwargs or DEFAULT_CLIENT_KWARGS)) as client:
                resp = await client.post(url, json=payload, headers=headers)
                await _raise_for_openai_error(resp)
                data = resp.json()

            total_latency_ms = (time.perf_counter() - start) * 1000
            usage = data.get("usage", {})
            input_tokens = _extract_input_tokens(usage, protocol)
            if protocol == "anthropic":
                tokens_generated = usage.get("output_tokens", 0) or 0
                content_tokens = tokens_generated  # Anthropic usage 不拆分思考 token
                response_content = "".join(
                    b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
                ) or None
            else:
                tokens_generated, reasoning_tokens, content_tokens = _reconcile_token_counts(
                    usage.get("completion_tokens", 0) or 0, _extract_reasoning_tokens(usage)
                )
                response_content = data.get("choices", [{}])[0].get("message", {}).get("content") or None
            actual_model = data.get("model", model)
            # Non-streaming: no meaningful TTFT
            ttft_ms = None
            content_ttft_ms = None

        # 有效速度：tokens_generated / 全程总耗时（含 TTFT）。
        # 网关隐藏思考（不流式转发 reasoning）时 TTFT≈思考耗时，若只按"首 token 之后"算，
        # 会把思考时间和思考 token 一起排除，得出发射速率（如 585 tok/s），偏离实际体感。
        # 主流基准（llmperf/lmsys 等）的 tokens/sec 均按全程 (tokens / e2e latency) 定义。
        tps = (tokens_generated / (total_latency_ms / 1000)) if tokens_generated > 0 else None
        # ITL：扣除首字前等待后的解码耗时（见 _compute_itl）
        itl_ms = _compute_itl(content_tokens, content_ttft_ms, total_latency_ms)
        # 思考耗时仅在流中出现过 reasoning 增量时才可测（ttft=首个 reasoning token，
        # content_ttft=首个正文 token）；否则网关只转正文，思考时间已混入 TTFT
        if content_ttft_ms is not None and ttft_ms is not None and reasoning_seen:
            thinking_ms = content_ttft_ms - ttft_ms
        else:
            thinking_ms = None

        return {
            "id": test_id,
            "base_url": base_url,
            "model": model,
            "actual_model": actual_model,
            "provider_id": provider_id,
            "provider_name": provider_name,
            "response_content": response_content,
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "ttft_ms": round(ttft_ms, 2) if ttft_ms is not None else None,
            "content_ttft_ms": round(content_ttft_ms, 2) if content_ttft_ms is not None else None,
            "total_latency_ms": round(total_latency_ms, 2),
            "tokens_generated": tokens_generated,
            "reasoning_tokens": reasoning_tokens,
            "content_tokens": content_tokens,
            "input_tokens": input_tokens,
            "thinking_ms": round(thinking_ms, 2) if thinking_ms is not None else None,
            "tps": round(tps, 2) if tps is not None else None,
            "itl_ms": round(itl_ms, 2) if itl_ms is not None else None,
            "success": True,
            "error_message": None,
            "created_at": created_at,
        }
    except Exception as e:
        total_latency_ms = (time.perf_counter() - start) * 1000
        # httpx 超时异常的 str() 为空串（历史数据里 32 个空 error_message 均为整 120s 超时），
        # 用类名兜底，至少能从结果里分辨超时
        message = str(e) or type(e).__name__
        return {
            "id": test_id,
            "base_url": base_url,
            "model": model,
            # 失败样本统一 actual_model 为占位 "error"，与批量失败路径一致，便于前端解析口径过滤
            "actual_model": "error",
            "provider_id": provider_id,
            "provider_name": provider_name,
            "response_content": None,
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "ttft_ms": None,
            "content_ttft_ms": None,
            "total_latency_ms": round(total_latency_ms, 2),
            "tokens_generated": 0,
            "reasoning_tokens": 0,
            "content_tokens": 0,
            "input_tokens": 0,
            "thinking_ms": None,
            "tps": None,
            "itl_ms": None,
            "success": False,
            "error_message": message,
            "created_at": created_at,
        }


async def execute_batch_tests(
    tests: list[dict],
    prompt: str,
    max_tokens: int | None,
    temperature: float | None,
    stream: bool,
    concurrency: int,
    iterations: int,
    schedule_id: str | None = None,
    max_rpm: int = -1,
    on_progress: Callable[[dict], Awaitable[None]] | None = None,
    sink: Callable[[dict], Awaitable[None]] | None = None,
    timeout: float | None = None,
    client_kwargs: dict | None = None,
) -> list[dict]:
    """批量执行测速。batch 端点与定时调度器、巡检 runner 共用。

    并发按 provider(base_url+api_key) 分桶：每个 provider 独占一个 Semaphore(concurrency)，
    不同 provider 互不挤占。桶内按模型 round-robin 交错展开（每轮迭代依次取各 model），
    使同 provider 各模型在相同并发密度下被测。返回每个测试的结果 dict（异常以失败结果兜底）。

    数据落地由调用方通过 sink callback 注入（逐条消费，每条完成后调用）。
    core 层不感知数据去向（SQLite / JSON / ...），这是两种部署形态正交的支点。
    不传 sink 时不在本函数内落地（仅返回 results）。
    """
    # 按 provider 分桶，桶内保留原始 model 顺序
    buckets: dict[tuple[str, str], list[dict]] = {}
    for t in tests:
        key = (t["base_url"], t.get("api_key", ""))
        buckets.setdefault(key, []).append(t)

    # 桶内按模型 round-robin 展开
    all_items: list[dict] = []
    for items in buckets.values():
        for _ in range(iterations):
            all_items.extend(items)

    semaphores = {key: asyncio.Semaphore(concurrency) for key in buckets}

    async def run_one(item: dict) -> dict:
        key = (item["base_url"], item.get("api_key", ""))
        await limiter.acquire(key, max_rpm)
        sem = semaphores[key]
        async with sem:
            try:
                return await run_speed_test(
                    base_url=item["base_url"],
                    api_key=item.get("api_key", ""),
                    model=item["model"],
                    prompt=prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stream=stream,
                    provider_id=item.get("provider_id", ""),
                    provider_name=item.get("provider_name", ""),
                    protocol=item.get("protocol", "openai"),
                    timeout=timeout,
                    client_kwargs=client_kwargs,
                )
            except Exception as e:
                return _batch_error_result(e, item, prompt, max_tokens, temperature)

    # 每完成一个测试回调一次进度（调度器据此更新 run_done/run_success 供前端 chip 展示）
    async def run_one_progress(it: dict) -> dict:
        r = await run_one(it)
        if on_progress is not None:
            await on_progress(r)
        return r

    results = await asyncio.gather(*[run_one_progress(it) for it in all_items], return_exceptions=True)
    results = [r if not isinstance(r, Exception) else _batch_error_result(r, it, prompt, max_tokens, temperature) for r, it in zip(results, all_items)]

    if sink is not None:
        for r in results:
            await sink(r)

    return results


def _batch_error_result(exc: BaseException, item: dict, prompt: str, max_tokens: int | None, temperature: float | None) -> dict:
    """构造批量测速的失败兜底结果。"""
    return {
        "id": str(uuid.uuid4()),
        "base_url": item.get("base_url", ""),
        "model": item.get("model", "error"),
        "actual_model": "error",
        "provider_id": item.get("provider_id", ""),
        "provider_name": item.get("provider_name", ""),
        "response_content": None,
        "content_ttft_ms": None,
        "reasoning_tokens": 0,
        "content_tokens": 0,
        "input_tokens": 0,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "ttft_ms": None,
        "total_latency_ms": 0,
        "tokens_generated": 0,
        "thinking_ms": None,
        "tps": None,
        "itl_ms": None,
        "success": False,
        "error_message": str(exc),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

