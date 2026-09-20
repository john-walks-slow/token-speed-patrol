# AGENTS.md — Token Speed Patrol

## 目标

LLM API 定时巡检（GitHub Actions）+ 静态看板（GitHub Pages）。零服务器、零构建。
核心价值是**测速指标口径正确**（TTFT / TPS / 思考时长 / token 拆分，兼容不同网关的 usage 统计口径）。

## 地图

- `backend/patrol_runner.py` — 巡检入口（`python -m backend.patrol_runner`），产 JSONL 到 `website/data/`
- `backend/patrol_config.py` — 巡检配置数据结构（PatrolConfig）
- `backend/speed_test.py` — 测速内核：流式/非流式、OpenAI/Anthropic 协议、usage 口径统一（`_reconcile_token_counts`）。数据落地由 sink callback 解耦
- `backend/rate_limit.py` — 令牌桶限流
- `website/index.html` — 看板（零构建纯 JS，原生 Canvas 图表），`data/` 存 JSONL 结果，push 即生效
- `config/patrol.json.example` — 配置示例（fork 后改名 patrol.json + 填 Secrets）
- `.github/workflows/patrol.yml` — 巡检 cron（每6h）+ 结果 commit
- `.github/workflows/deploy-website.yml` — Pages 部署（workflow_run 联动 patrol 完成）

## 开发与调试

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest backend/ -q   # 测试（必须全绿）
python -m http.server 8899 -d website   # 看板本地预览
```

## 规范

- usage 口径有两种：completion_tokens 含 reasoning（OpenAI/DeepSeek）与不含（Gemini 系网关转发）。
  一切 token 计算必须经 `_reconcile_token_counts`，不要直接相减。
- 思考时长只在流中真实出现 reasoning 增量时可测；网关只转正文时 thinking_ms 必须为 None。
- 统计中失败样本（actual_model='error'）按输入 modelid 归组计入成功率；数值指标先过滤 success。
- 本仓库与桌面版 [token-speed](https://github.com/john-walks-slow/token-speed) 共用测速内核。
  `speed_test.py` / `rate_limit.py` / `url_utils.py` / `patrol_config.py` 为同源文件，
  **改动 core 需手动同步另一仓库**（纯拷贝策略，勿引入构建依赖）。
- `test_load_patrol_config_example` 断言了 example 的具体内容，改 `patrol.json.example` 时须同步改测试。
