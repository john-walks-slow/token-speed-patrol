# OpenCode Zen 免费模型接入巡检（261006）

## 目标

让 GH Actions 巡检（零服务器、直连）也能测 OpenCode Zen 的免费模型速度，
复用本机 `opencode2api` 反代所复刻的免费层握手，而不是依赖本机反代/隧道。

## 关键发现

- OpenCode Zen 免费层（`*-free`、`big-pickle`）对匿名客户端开放，但上游按
  「请求是否来自 OpenCode CLI」做门禁，缺任一要素即 `403 FreeTierError`：
  1. `x-opencode-session` / `x-opencode-request` 必须形如 `ses_/msg_` + 12 位 hex + 14 位 base62；
  2. `User-Agent` 声明 `opencode/<version>` 且版本 ≥ 1.18.0；
  3. body 必须补齐 `bash/glob/grep/read` 四件占位工具，且 `stream` 强制 `true`。
- `/v1/models` 无需鉴权即可拉全量列表，故可用 `discover + whitelist` 自动跟随免费模型。
- 本机家宽 IP 的免费池额度已被 `opencode2api` 耗尽（实测 10/14 模型 429），
  因此**经本机反代/隧道暴露给 GH 会测到被限流的家宽出口**，数据不可比。
  直连方案让 GH runner 用每轮不同的新 IP，规避该问题。

## 实现

- `backend/speed_test.py`：新增 `protocol: "opencode"` 特判——按上述三要素构造
  请求头（匿名 `Bearer public`，可被 `api_key` 覆盖）与 body（占位工具 + 强制流式），
  响应仍走 OpenAI SSE 解析路径。
- `backend/patrol_runner.py`：`discover_models` 在无 key 时不再发空的
  `Authorization: Bearer `（httpx 会拒绝），支持匿名发现端点。
- `config/patrol.json`：`OpenCode Zen` target 改为直连 `https://opencode.ai/zen/v1`，
  `protocol: "opencode"`，`discover: true` + `whitelist: [".*-free", "big-pickle"]`；
  `OPENCODE_ZEN_KEY` 可不配。
- `README.md`：新增「OpenCode Zen 免费模型（直连）」小节与免费端点表格行。

## 验证

- 单测：`python -m pytest backend/ -q` → 15 passed（新增握手格式/头/工具用例）。
- 端到端（本机经 mihomo 代理直连 zen）：
  `discover 86 models → 14 after filter`，4 成功并产出完整 TTFT / TPS / 思考时长指标；
  其余 10 个为家宽 IP 的 `429 FreeUsageLimitError`（预期，GH 新 IP 更宽松）。
- 待 GH 侧确认：手动触发一次 workflow，确认 dashboard 出现 OpenCode Zen 行、指标正常。

## 风险 / 后续

- 免费层门禁为逆向所得，上游调整会失效；`protocol=opencode` 属与桌面版共享的
  core（`speed_test.py`），需手动同步到 [token-speed](https://github.com/john-walks-slow/token-speed) 仓库。
- 免费池按 IP 限流，若某 GH IP 段被持续限流，连续 5 轮失败会触发自动黑名单；
  届时可删 `website/data/blacklist.json` 对应条目。
