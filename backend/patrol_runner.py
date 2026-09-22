"""巡检 runner：GH Actions 定时巡检入口（PATROL adapter）。

`python -m backend.patrol_runner` —— 不依赖 FastAPI 事件循环，
读 config/patrol.json → 组装 tests → execute_batch_tests(sink=json_sink)
→ 写 JSON 到 website/data/。

数据落地由 json_sink 注入（逐条收集，结束后一次性写文件）。core 层
（speed_test.py）不感知数据去向，与本地 App 共用同一测速内核。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone

import httpx

from .patrol_config import load_patrol_config
from .speed_test import DEFAULT_CLIENT_KWARGS, execute_batch_tests

# 巡检结果目录：放 website/data/ 下，随 Pages 一起发布
DEFAULT_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "website", "data",
)

# 自动黑名单文件（与结果同目录，随仓库 commit 持久化，GH Actions 无状态下的唯一存储）
BLACKLIST_FILE = "blacklist.json"
# 连续失败达到该次数的模型下一轮起被剔除；成功一次即清零恢复
BLACKLIST_THRESHOLD = 5


def load_auto_blacklist(data_dir: str) -> dict:
    """读取自动黑名单。返回 {"provider_name/model": {...}} 结构；缺失/损坏返回空。"""
    path = os.path.join(data_dir, BLACKLIST_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            body = json.load(f)
        return body.get("failures", {})
    except (OSError, json.JSONDecodeError):
        return {}


def update_auto_blacklist(data_dir: str, results: list[dict], run_finished: str) -> dict:
    """按本轮结果更新连续失败计数，返回当前已拉黑集合（"provider/model"）。

    失败 +1，成功清零；达到 BLACKLIST_THRESHOLD 记入黑名单。写回 blacklist.json。
    429 等间歇性失败通常不会连续 5 次，真连续 5 次的多为下线/无权限/彻底超时的尸体模型。
    """
    failures = load_auto_blacklist(data_dir)
    now_blacklisted = set()
    for r in results:
        key = f"{r.get('provider_name')}/{r.get('model')}"
        if r.get("success"):
            failures.pop(key, None)
            continue
        rec = failures.setdefault(key, {"consecutive_failures": 0})
        rec["consecutive_failures"] += 1
        rec["last_error"] = (r.get("error_message") or "")[:200]
        if rec["consecutive_failures"] >= BLACKLIST_THRESHOLD:
            rec.setdefault("blacklisted_at", run_finished)
        if "blacklisted_at" in rec:
            now_blacklisted.add(key)
    with open(os.path.join(data_dir, BLACKLIST_FILE), "w", encoding="utf-8") as f:
        json.dump({"failures": failures}, f, ensure_ascii=False, indent=2)
    return now_blacklisted


async def discover_models(base_url: str, api_key: str, timeout: float, discover_url: str = "") -> list[str]:
    """从发现端点拉上游全量模型 id；失败返回空列表（退回显式 models）。

    兼容两种响应形状：OpenAI `{data: [{id}]}` 与 Cloudflare `{result: [{id: uuid, name}]}`。
    CF 的 id 是内部 UUID，模型名在 name 字段，故 name 优先。
    """
    url = (discover_url or base_url.rstrip("/") + "/models").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=timeout, headers={"Authorization": f"Bearer {api_key}"},
                                     **DEFAULT_CLIENT_KWARGS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            body = resp.json()
        items = body.get("data") or body.get("result") or []
        return [m.get("name") or m.get("id") for m in items if m.get("name") or m.get("id")]
    except Exception as e:
        print(f"patrol: discover {base_url} failed: {e}", file=sys.stderr)
        return []


async def run_patrol(config_path: str, data_dir: str = DEFAULT_DATA_DIR,
                     only_providers: list[str] | None = None) -> str:
    """执行一次巡检，结果写入 data_dir 下的按天 JSONL 文件。

    only_providers: 非空时仅巡检列表内的 provider_name（手动按需触发用）。
    返回写入的文件路径（跳过时返回空串）。
    """
    cfg = load_patrol_config(config_path)

    if only_providers:
        cfg.targets = [t for t in cfg.targets if t.provider_name in only_providers]
        if not cfg.targets:
            print(f"patrol: no target matches {only_providers}, skipping", file=sys.stderr)
            return ""

    # discover=true 的 target 先拉上游全量模型列表（与显式 models 并集后过过滤规则）
    timeout = cfg.timeout or 120.0
    discovered: dict[str, list[str]] = {}
    for t in cfg.targets:
        if t.discover:
            discovered[t.provider_name] = await discover_models(
                t.resolve_base_url(), t.resolve_api_key(), timeout, t.discover_url)
            if discovered[t.provider_name]:
                after = len(t.filter_models(discovered[t.provider_name]))
                print(f"patrol: discover {t.provider_name}: {len(discovered[t.provider_name])} models, {after} after filter", file=sys.stderr)

    tests = cfg.to_tests(discovered)

    # 上一轮累积的自动黑名单：连续失败达阈值的模型不再纳入本轮巡检
    # （未达阈值的失败记录只是计数，不拦截——429/503 间歇失败照常测）
    auto_blacklist = {k for k, rec in load_auto_blacklist(data_dir).items()
                      if "blacklisted_at" in rec}
    if auto_blacklist:
        before = len(tests)
        tests = [t for t in tests
                 if f"{t['provider_name']}/{t['model']}" not in auto_blacklist]
        print(f"patrol: auto-blacklist {len(auto_blacklist)} entries, {before} -> {len(tests)} tests", file=sys.stderr)

    if not tests:
        print("patrol: no targets configured, skipping", file=sys.stderr)
        return ""

    run_id = str(uuid.uuid4())
    run_started = datetime.now(timezone.utc).isoformat()
    collected: list[dict] = []

    async def json_sink(result: dict) -> None:
        collected.append(result)

    results = await execute_batch_tests(
        tests,
        prompt=cfg.prompt,
        max_tokens=cfg.max_tokens,
        temperature=cfg.temperature,
        stream=cfg.stream,
        concurrency=cfg.concurrency,
        iterations=cfg.iterations,
        max_rpm=cfg.max_rpm,
        timeout=cfg.timeout,
        sink=json_sink,
    )

    run_finished = datetime.now(timezone.utc).isoformat()
    ok = sum(1 for r in results if r.get("success"))
    status = "success" if ok == len(results) else ("partial" if ok > 0 else "failed")

    # 按天分文件：website/data/YYYY-MM-DD.jsonl
    # 每行一个 run 的完整结果数组，便于静态看板按天加载
    os.makedirs(data_dir, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = os.path.join(data_dir, f"{day}.jsonl")

    # 本轮结果更新自动黑名单（连续失败计数 + 拉黑/恢复），与结果文件同目录持久化
    update_auto_blacklist(data_dir, results, run_finished)

    # 私有端点（base_url 引用环境变量）不落盘：结果中以占位符替代真实 URL。
    # error_message 里 httpx 异常可能带完整请求 URL，一并替换。
    private_map = {t.resolve_base_url(): "(private)" for t in cfg.targets if t.is_private}

    def scrub(r: dict) -> None:
        url = r.get("base_url")
        if url in private_map:
            r["base_url"] = private_map[url]
            if r.get("error_message"):
                r["error_message"] = r["error_message"].replace(url, private_map[url])

    for r in collected:
        scrub(r)
    for r in results:
        scrub(r)

    run_record = {
        "run_id": run_id,
        "started_at": run_started,
        "finished_at": run_finished,
        "status": status,
        "total": len(results),
        "success": ok,
        "failed": len(results) - ok,
        "results": results,
    }

    with open(out_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(run_record, ensure_ascii=False) + "\n")

    print(f"patrol: {ok}/{len(results)} success → {out_path}", file=sys.stderr)
    return out_path


def apply_extra_env(raw: str) -> None:
    """解析多行 KEY=VALUE 注入环境变量（GH secret PATROL_EXTRA_ENV）。

    setdefault 使真实环境变量优先，本地调试可直接覆盖。
    """
    for line in raw.splitlines():
        line = line.strip()
        if "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def main() -> None:
    apply_extra_env(os.environ.get("PATROL_EXTRA_ENV", ""))
    config_path = os.environ.get("PATROL_CONFIG", "config/patrol.json")
    data_dir = os.environ.get("PATROL_DATA_DIR", DEFAULT_DATA_DIR)
    raw = os.environ.get("PATROL_PROVIDERS", "").strip()
    only = [p.strip() for p in raw.split(",") if p.strip()] or None
    asyncio.run(run_patrol(config_path, data_dir, only_providers=only))


if __name__ == "__main__":
    main()
