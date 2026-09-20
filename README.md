# Token Speed Patrol

LLM API 定时巡检 + 静态看板，跑在 GitHub Actions 上，零服务器、零成本。

GitHub Actions 定时对你的 LLM API 服务商测速（TTFT / TPS / 思考时长 / 成功率），结果以 JSONL 提交回仓库，GitHub Pages 发布看板展示趋势。所有数据公开可追溯——每一行历史都能在 git 里找到。

![看板](docs/patrol-dashboard.png)

## 快速开始

1. **Fork 本仓库**
2. **启用 Actions**：你的 fork → Settings → Actions → Allow all actions（fork 默认禁用）
3. **配置 API 密钥**：Settings → Secrets and variables → Actions，按 `config/patrol.json` 中 `api_key_env` 字段添加 secrets（用不到的 target 可以直接从 `patrol.json` 删掉）
4. **触发首次巡逻**：Actions → Token Speed Patrol → Run workflow
5. **开启 Pages**：Settings → Pages → Source: GitHub Actions

看板地址：`https://<你的用户名>.github.io/token-speed-patrol/`

## 内置免费巡逻目标

`config/patrol.json` 自带 4 个 target：Groq、NVIDIA NIM、Google Gemini、OpenRouter。

| Secret 名 | 从哪获取密钥 |
|---|---|
| `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) — 永久免费层，无需信用卡 |
| `NVIDIA_API_KEY` | [build.nvidia.com](https://build.nvidia.com/settings) — 免费试用额度，无需信用卡 |
| `GEMINI_API_KEY` | [aistudio.google.com](https://aistudio.google.com/apikey) — 免费层，无需信用卡 |
| `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) — `:free` 模型免费（50 请求/天） |

密钥**绝不落盘**——`patrol.json` 只存环境变量名，runner 从 `os.environ` 解析，结果 JSON 不含 `api_key`；私有端点 URL 在结果中脱敏为 `(private)`。

## 自定义巡逻目标

编辑 `config/patrol.json`：

```json
{
  "prompt": "Hello, tell me a short story in 3 sentences.",
  "max_tokens": 256,
  "stream": true,
  "targets": [
    {
      "provider_name": "你的服务商名",
      "base_url": "https://api.xxx.com/v1",
      "api_key_env": "YOUR_SECRET_NAME",
      "protocol": "openai",
      "models": ["model-id-1", "model-id-2"]
    }
  ]
}
```

- `protocol` 支持 `openai` 与 `anthropic`（Anthropic 原生端点用后者）。
- 新增服务商时，在 `patrol.yml` 的 `env:` 段加对应的 `${{ secrets.XXX }}` 映射。
- **动态模型发现**：`"discover": true` 时从上游 `{base_url}/models`（或 `discover_url`）拉全量模型列表，与手写 `models` 并集后过 `whitelist` / `blacklist`（regex，fullmatch）。上游增删模型自动跟随。
- **私有端点**：`base_url` 填 `"$MY_BASE_URL"` 即从环境变量注入（配合 secrets / `PATROL_EXTRA_ENV`），结果自动脱敏。
- **巡检周期**：`patrol.yml` 的 `schedule.cron`（默认每 6 小时）。手动触发时可填 `providers` 输入，只测指定服务商。

## 测速口径

- **TTFT**：首个可见 token（含 reasoning）延迟；**content TTFT**：首个正文 token。
- **TPS** = tokens_generated / 端到端总耗时（与 llmperf/lmsys 等主流基准一致）。
- **ITL**：扣除首字等待后的纯解码发射间隔。
- **思考时长**：仅当流中真实出现 reasoning 增量时可测；网关只转正文时为 null。
- token 数兼容两种 usage 口径（completion 含/不含 reasoning），经 `_reconcile_token_counts` 统一。

## 本地开发

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest backend/ -q       # 后端测试
python -m http.server 8899 -d website   # 看板本地预览
```

## 相关项目

- [token-speed](https://github.com/john-walks-slow/token-speed) — 桌面版：多服务商管理、即时测速、本地历史统计（Windows）。

## License

MIT
