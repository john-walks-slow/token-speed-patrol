"""patrol_config / patrol_runner 单元测试。"""

import json
import os
import tempfile

import pytest

from .patrol_config import PatrolConfig, PatrolTarget, load_patrol_config
from .speed_test import _compute_itl


def test_compute_itl():
    """ITL = (总耗时 - content_ttft) / (content_tokens - 1)，TPOT 标准口径。"""
    # 首个正文 token 在 100ms 到达，之后 9 个 token 分摊 900ms -> 100ms/token
    assert _compute_itl(10, 100.0, 1000.0) == 100.0
    # 只有 1 个正文 token：无发射间隔，不可测
    assert _compute_itl(1, 100.0, 1000.0) is None
    # 非流式 / 无正文
    assert _compute_itl(100, None, 1000.0) is None
    assert _compute_itl(0, 100.0, 1000.0) is None
    # 异常数据：总耗时小于 content_ttft（时钟或网关回包乱序）
    assert _compute_itl(10, 1100.0, 1000.0) is None


def test_load_patrol_config_example():
    """仓库自带 patrol.json 能正确加载，字段一一对应。"""
    cfg = load_patrol_config("config/patrol.json")
    assert len(cfg.targets) == 7
    assert cfg.targets[0].provider_name == "Groq"
    assert cfg.targets[0].api_key_env == "GROQ_API_KEY"
    assert cfg.targets[0].protocol == "openai"
    assert "openai/gpt-oss-20b" in cfg.targets[0].models
    # NVIDIA NIM target（动态发现）
    assert cfg.targets[1].provider_name == "NVIDIA NIM"
    assert cfg.targets[1].base_url == "https://integrate.api.nvidia.com/v1"
    assert cfg.targets[1].discover is True
    # Google Gemini target
    assert cfg.targets[2].provider_name == "Google Gemini"
    assert cfg.targets[2].base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"
    # Cloudflare target（account id 直写，key 走 env；discover_url 自定义）
    assert cfg.targets[3].provider_name == "Cloudflare Workers AI"
    assert cfg.targets[3].api_key_env == "CLOUDFLARE_API_KEY"
    assert "models/search" in cfg.targets[3].discover_url
    # OpenRouter target（discover + whitelist/blacklist + 显式 :free 兜底列表）
    assert cfg.targets[4].provider_name == "OpenRouter"
    assert cfg.targets[4].discover is True
    assert cfg.targets[4].whitelist == [".*:free"]
    assert len(cfg.targets[4].models) > 0
    assert cfg.targets[5].provider_name == "ModelScope"
    assert cfg.targets[5].api_key_env == "MODELSCOPE_API_KEY"
    # Kilo Relay target（models 为空则本轮跳过，经私有中转，README 启用）
    assert cfg.targets[6].provider_name == "Kilo Relay"
    assert cfg.targets[6].base_url == "$KILO_RELAY_URL"
    assert cfg.targets[6].models == []
    assert cfg.stream is True
    assert cfg.max_tokens == 1024
    assert cfg.temperature is None


def test_to_tests_expands_models(monkeypatch):
    """每个 model 展开为一条 test，api_key 从 env 解析。"""
    monkeypatch.setenv("FAKE_KEY", "sk-123")
    cfg = PatrolConfig(
        targets=[
            PatrolTarget(
                provider_name="P1", base_url="https://api.p1.com/v1",
                api_key_env="FAKE_KEY", protocol="openai",
                models=["m1", "m2"],
            )
        ]
    )
    tests = cfg.to_tests()
    assert len(tests) == 2
    assert tests[0]["model"] == "m1"
    assert tests[0]["api_key"] == "sk-123"
    assert tests[0]["provider_name"] == "P1"
    assert tests[0]["protocol"] == "openai"


def test_resolve_api_key_missing_returns_empty(monkeypatch):
    """环境变量缺失时返回空串（core 层会用空串请求，失败兜底）。"""
    monkeypatch.delenv("NO_SUCH_KEY", raising=False)
    t = PatrolTarget(provider_name="P", base_url="x", api_key_env="NO_SUCH_KEY")
    assert t.resolve_api_key() == ""


def test_resolve_base_url_from_env(monkeypatch):
    """base_url 支持 $ENV / ${ENV} 引用环境变量；缺失返回空串；普通 URL 原样。"""
    monkeypatch.setenv("MY_URL", "https://private.example.com/v1")
    t = PatrolTarget(provider_name="P", base_url="$MY_URL", api_key_env="K")
    assert t.resolve_base_url() == "https://private.example.com/v1"
    assert t.is_private is True

    t2 = PatrolTarget(provider_name="P", base_url="${MY_URL}", api_key_env="K")
    assert t2.resolve_base_url() == "https://private.example.com/v1"

    monkeypatch.delenv("NO_SUCH_URL", raising=False)
    t3 = PatrolTarget(provider_name="P", base_url="$NO_SUCH_URL", api_key_env="K")
    assert t3.resolve_base_url() == ""

    t4 = PatrolTarget(provider_name="P", base_url="https://api.p1.com/v1", api_key_env="K")
    assert t4.resolve_base_url() == "https://api.p1.com/v1"
    assert t4.is_private is False


def test_private_url_placeholder(monkeypatch, tmp_path):
    """私有 target 的结果 JSONL 中 base_url 替换为 (private)，公有原样保留。"""
    import asyncio

    async def fake_execute(tests, prompt, max_tokens, temperature, stream,
                           concurrency, iterations, max_rpm=-1,
                           on_progress=None, sink=None, timeout=None):
        results = [{
            "id": f"r{i}", "base_url": t["base_url"], "model": t["model"],
            "provider_name": t["provider_name"], "success": False,
            "error_message": f"ConnectError: request to {t['base_url']} failed",
            "tps": None, "created_at": "2026-09-20T10:00:00+00:00",
        } for i, t in enumerate(tests)]
        if sink:
            for r in results:
                await sink(r)
        return results

    monkeypatch.setattr("backend.patrol_runner.execute_batch_tests", fake_execute)

    cfg = {
        "targets": [
            {"provider_name": "Priv", "base_url": "$MY_URL",
             "api_key_env": "TEST_KEY", "protocol": "openai", "models": ["m1"]},
            {"provider_name": "Pub", "base_url": "https://api.pub.com/v1",
             "api_key_env": "TEST_KEY", "protocol": "openai", "models": ["m2"]},
        ]
    }
    cfg_path = tmp_path / "patrol.json"
    cfg_path.write_text(json.dumps(cfg))
    monkeypatch.setenv("TEST_KEY", "sk-fake")
    monkeypatch.setenv("MY_URL", "https://private.example.com/v1")

    from .patrol_runner import run_patrol
    out = asyncio.run(run_patrol(str(cfg_path), str(tmp_path / "data")))
    with open(out) as f:
        data = json.loads(f.readline())
    urls = {r["provider_name"]: r["base_url"] for r in data["results"]}
    assert urls["Priv"] == "(private)"
    assert urls["Pub"] == "https://api.pub.com/v1"
    # error_message 中私有 URL 一并脱敏，公有不受影响
    errs = {r["provider_name"]: r["error_message"] for r in data["results"]}
    assert "https://private.example.com" not in errs["Priv"]
    assert errs["Priv"] == "ConnectError: request to (private) failed"
    assert errs["Pub"] == "ConnectError: request to https://api.pub.com/v1 failed"


def test_extra_env_parsing(monkeypatch):
    """PATROL_EXTRA_ENV 多行 KEY=VALUE 注入环境；已有变量不被覆盖。"""
    from .patrol_runner import apply_extra_env

    monkeypatch.setenv("EXISTING", "original")
    monkeypatch.delenv("EXTRA_A", raising=False)
    monkeypatch.delenv("EXTRA_B", raising=False)
    apply_extra_env("EXTRA_A=1\n  EXTRA_B = v2 \nEXISTING=overridden\nno-equals-line")
    assert os.environ["EXTRA_A"] == "1"
    assert os.environ["EXTRA_B"] == "v2"
    assert os.environ["EXISTING"] == "original"


def test_patrol_runner_writes_jsonl(monkeypatch, tmp_path):
    """patrol_runner 用 mock execute_batch_tests 验证 JSONL 产出。"""
    import asyncio

    async def fake_execute(tests, prompt, max_tokens, temperature, stream,
                           concurrency, iterations, max_rpm=-1,
                           on_progress=None, sink=None, timeout=None):
        results = [{
            "id": "r1", "base_url": tests[0]["base_url"], "model": tests[0]["model"],
            "actual_model": tests[0]["model"], "provider_id": "", "provider_name": tests[0]["provider_name"],
            "response_content": "hi", "prompt": prompt,
            "max_tokens": max_tokens, "temperature": temperature,
            "ttft_ms": 100.0, "content_ttft_ms": 110.0, "total_latency_ms": 500.0,
            "tokens_generated": 10, "reasoning_tokens": 0, "content_tokens": 10,
            "input_tokens": 5, "thinking_ms": None, "tps": 20.0,
            "success": True, "error_message": None,
            "created_at": "2026-09-20T10:00:00+00:00",
        }]
        if sink:
            for r in results:
                await sink(r)
        return results

    monkeypatch.setattr("backend.patrol_runner.execute_batch_tests", fake_execute)

    cfg = {
        "prompt": "hi", "max_tokens": 100, "temperature": None,
        "stream": True, "concurrency": 1, "iterations": 1, "max_rpm": -1,
        "targets": [{
            "provider_name": "TestP", "base_url": "https://api.test.com/v1",
            "api_key_env": "TEST_KEY", "protocol": "openai", "models": ["m1"]
        }]
    }
    cfg_path = tmp_path / "patrol.json"
    cfg_path.write_text(json.dumps(cfg))
    monkeypatch.setenv("TEST_KEY", "sk-fake")

    data_dir = tmp_path / "data"
    from .patrol_runner import run_patrol
    out = asyncio.run(run_patrol(str(cfg_path), str(data_dir)))

    assert out.endswith(".jsonl")
    with open(out) as f:
        line = f.readline()
        data = json.loads(line)
    assert data["status"] == "success"
    assert data["total"] == 1
    assert data["results"][0]["tps"] == 20.0
    # 结果不含 api_key（脱敏验证）
    assert "api_key" not in data["results"][0]


def test_filter_models_whitelist_blacklist():
    """并集 → whitelist 匹配保留（空=全保留）→ blacklist 剔除，regex 全匹配。"""
    t = PatrolTarget(
        provider_name="P", base_url="x", api_key_env="K",
        models=["hand-written", "z-ai/glm-5.2:free"],
        whitelist=[r".*:free", r"hand-.*"],
        blacklist=[r".*inkling.*"],
    )
    final = t.filter_models(["z-ai/glm-5.2:free", "thinkingmachines/inkling:free", "paid/model"])
    assert "hand-written" in final
    assert "z-ai/glm-5.2:free" in final
    assert "thinkingmachines/inkling:free" not in final
    assert "paid/model" not in final


def test_filter_models_blacklist_only_and_dedup():
    """无 whitelist 时全保留，仅 blacklist 剔除；并集去重保持顺序。"""
    t = PatrolTarget(
        provider_name="P", base_url="x", api_key_env="K",
        models=["m1", "m2"],
        blacklist=[r"m2"],
    )
    final = t.filter_models(["m2", "m3"])
    assert final == ["m1", "m3"]


def test_to_tests_with_discovered(monkeypatch):
    """discover 结果经 filter_models 并入 tests 展开。"""
    monkeypatch.setenv("FAKE_KEY", "sk-123")
    cfg = PatrolConfig(
        targets=[PatrolTarget(
            provider_name="P1", base_url="https://api.p1.com/v1",
            api_key_env="FAKE_KEY", models=["m1"],
            blacklist=[r"m3"],
        )]
    )
    tests = cfg.to_tests({"P1": ["m2", "m3"]})
    assert [t["model"] for t in tests] == ["m1", "m2"]


def test_update_auto_blacklist_accumulate_and_recover(tmp_path):
    """失败累加、成功清零；达到阈值拉黑；恢复后记录删除。"""
    from .patrol_runner import BLACKLIST_THRESHOLD, update_auto_blacklist

    def result(model, ok, err="boom"):
        return {"provider_name": "P", "model": model, "success": ok,
                "error_message": None if ok else err}

    # 连续失败 N-1 次：未拉黑
    for i in range(BLACKLIST_THRESHOLD - 1):
        blacklisted = update_auto_blacklist(str(tmp_path), [result("m1", False)], "t")
        assert blacklisted == set()
    body = json.loads((tmp_path / "blacklist.json").read_text())
    assert body["failures"]["P/m1"]["consecutive_failures"] == BLACKLIST_THRESHOLD - 1
    assert "blacklisted_at" not in body["failures"]["P/m1"]

    # 第 N 次失败：拉黑，blacklisted_at 只记第一次
    blacklisted = update_auto_blacklist(str(tmp_path), [result("m1", False)], "t-final")
    assert blacklisted == {"P/m1"}
    body = json.loads((tmp_path / "blacklist.json").read_text())
    assert body["failures"]["P/m1"]["blacklisted_at"] == "t-final"

    # 再次失败：仍在黑名单，blacklisted_at 不变
    blacklisted = update_auto_blacklist(str(tmp_path), [result("m1", False)], "t-more")
    assert blacklisted == {"P/m1"}
    body = json.loads((tmp_path / "blacklist.json").read_text())
    assert body["failures"]["P/m1"]["blacklisted_at"] == "t-final"

    # 成功一次：清零，记录删除，出黑名单
    blacklisted = update_auto_blacklist(str(tmp_path), [result("m1", True)], "t-ok")
    assert blacklisted == set()
    body = json.loads((tmp_path / "blacklist.json").read_text())
    assert "P/m1" not in body["failures"]

    # last_error 截断到 200 字符
    update_auto_blacklist(str(tmp_path), [result("m2", False, err="x" * 500)], "t")
    body = json.loads((tmp_path / "blacklist.json").read_text())
    assert len(body["failures"]["P/m2"]["last_error"]) == 200


def test_run_patrol_filters_auto_blacklisted(monkeypatch, tmp_path):
    """黑名单内的模型本轮不产生 test，也不会进结果。"""
    import asyncio

    seen_tests = []

    async def fake_execute(tests, prompt, max_tokens, temperature, stream,
                           concurrency, iterations, max_rpm=-1,
                           on_progress=None, sink=None, timeout=None):
        seen_tests.extend(tests)
        results = [{
            "id": f"r{i}", "base_url": t["base_url"], "model": t["model"],
            "provider_name": t["provider_name"], "success": True,
            "tps": 1.0, "created_at": "2026-09-23T10:00:00+00:00",
        } for i, t in enumerate(tests)]
        if sink:
            for r in results:
                await sink(r)
        return results

    monkeypatch.setattr("backend.patrol_runner.execute_batch_tests", fake_execute)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "blacklist.json").write_text(json.dumps(
        {"failures": {"TestP/m1": {"consecutive_failures": 5,
                                    "last_error": "HTTP 404",
                                    "blacklisted_at": "2026-09-22T00:00:00+00:00"}}}))

    cfg = {
        "prompt": "hi", "max_tokens": 100, "temperature": None,
        "stream": True, "concurrency": 1, "iterations": 1, "max_rpm": -1,
        "targets": [{
            "provider_name": "TestP", "base_url": "https://api.test.com/v1",
            "api_key_env": "TEST_KEY", "protocol": "openai", "models": ["m1", "m2"]
        }]
    }
    cfg_path = tmp_path / "patrol.json"
    cfg_path.write_text(json.dumps(cfg))
    monkeypatch.setenv("TEST_KEY", "sk-fake")

    from .patrol_runner import run_patrol
    out = asyncio.run(run_patrol(str(cfg_path), str(data_dir)))

    assert [t["model"] for t in seen_tests] == ["m2"]
    with open(out) as f:
        data = json.loads(f.readline())
    assert [r["model"] for r in data["results"]] == ["m2"]
    # m2 成功后 blacklist.json 里无 m2 记录；m1 未参与本轮，原记录保留
    body = json.loads((data_dir / "blacklist.json").read_text())
    assert "TestP/m2" not in body["failures"]
    assert "TestP/m1" in body["failures"]
