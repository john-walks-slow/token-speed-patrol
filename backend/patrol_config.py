"""巡检配置：静态文件 + 环境变量密钥注入的数据结构。

core 层契约——本地 App（从 SQLite 组装）与 GH 巡检（从 patrol.json 加载）
共用同一结构，加载方式各自由 adapter 决定。

密钥不落盘：配置只存环境变量名（api_key_env），由调用方从 os.environ 解析成
明文传入 core 的 run_speed_test，巡检结果 JSON 不含 api_key。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


@dataclass
class PatrolTarget:
    provider_name: str
    base_url: str
    api_key_env: str  # 环境变量名，不存明文
    protocol: str = "openai"
    models: list[str] = field(default_factory=list)
    # 动态发现：true 时从 {base_url}/models 拉上游全量列表，与显式 models 并集
    discover: bool = False
    # 覆盖发现端点（默认 {base_url}/models）；用于列表路由不在 OpenAI 兼容路径下的 provider
    discover_url: str = ""
    # 白/黑名单均为 regex，作用于并集：先 whitelist 匹配保留（空 = 全保留），
    # 再 blacklist 剔除匹配项，剩下的为最终模型集
    whitelist: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)

    def resolve_api_key(self) -> str:
        """从环境变量读取密钥；缺失返回空串（core 层会用空串发起请求，失败兜底）。"""
        return os.environ.get(self.api_key_env, "")

    def resolve_base_url(self) -> str:
        """base_url 支持 "$ENV_NAME" / "${ENV_NAME}" 形式引用环境变量（私有端点不落盘）。

        普通 URL 原样返回；环境变量缺失返回空串（请求失败兜底，与 key 缺失一致）。
        """
        url = self.base_url
        if url.startswith("${") and url.endswith("}"):
            return os.environ.get(url[2:-1], "")
        if url.startswith("$"):
            return os.environ.get(url[1:], "")
        return url

    @property
    def is_private(self) -> bool:
        """base_url 引用环境变量即为私有端点，公开结果中以占位符替代。"""
        return self.base_url.startswith("$")

    def filter_models(self, candidates: list[str]) -> list[str]:
        """合并显式 models 与候选（发现列表），过 whitelist → blacklist 得最终集。"""
        merged = list(dict.fromkeys(self.models + candidates))
        keep = [re.compile(p) for p in self.whitelist]
        drop = [re.compile(p) for p in self.blacklist]
        if keep:
            merged = [m for m in merged if any(k.fullmatch(m) for k in keep)]
        merged = [m for m in merged if not any(b.fullmatch(m) for b in drop)]
        return merged


@dataclass
class PatrolConfig:
    prompt: str = "Hello, tell me a short story in 3 sentences."
    max_tokens: int | None = None
    temperature: float | None = None
    stream: bool = True
    concurrency: int = 1
    iterations: int = 1
    max_rpm: int = -1
    timeout: float | None = None
    targets: list[PatrolTarget] = field(default_factory=list)

    def to_tests(self, discovered: dict[str, list[str]] | None = None) -> list[dict]:
        """展开为 execute_batch_tests 所需的 tests 列表（每 model 一条）。

        discovered: provider_name → 动态发现的模型列表（runner 预先拉取），
        与显式 models 并集后过 whitelist/blacklist。缺省时仅用显式 models。
        api_key 在此解析为明文，传入 core；结果 dict 不含 api_key，不落盘。
        """
        discovered = discovered or {}
        tests: list[dict] = []
        for t in self.targets:
            api_key = t.resolve_api_key()
            base_url = t.resolve_base_url()
            for m in t.filter_models(discovered.get(t.provider_name, [])):
                tests.append({
                    "model": m,
                    "base_url": base_url,
                    "api_key": api_key,
                    "provider_id": "",  # 巡检无 SQLite provider 实体
                    "provider_name": t.provider_name,
                    "protocol": t.protocol,
                })
        return tests


def load_patrol_config(path: str) -> PatrolConfig:
    """从 JSON 文件加载巡检配置（GH adapter 用）。

    JSON 结构与 PatrolConfig/ParamTarget 字段一一对应，snake_case。
    """
    import json

    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    targets = [
        PatrolTarget(
            provider_name=t["provider_name"],
            base_url=t["base_url"],
            api_key_env=t["api_key_env"],
            protocol=t.get("protocol", "openai"),
            models=t.get("models", []),
            discover=t.get("discover", False),
            discover_url=t.get("discover_url", ""),
            whitelist=t.get("whitelist", []),
            blacklist=t.get("blacklist", []),
        )
        for t in raw.get("targets", [])
    ]
    return PatrolConfig(
        prompt=raw.get("prompt", PatrolConfig().prompt),
        max_tokens=raw.get("max_tokens"),
        temperature=raw.get("temperature"),
        stream=raw.get("stream", True),
        concurrency=raw.get("concurrency", 1),
        iterations=raw.get("iterations", 1),
        max_rpm=raw.get("max_rpm", -1),
        timeout=raw.get("timeout"),
        targets=targets,
    )
