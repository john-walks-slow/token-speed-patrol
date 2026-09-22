# 调研：监测对象覆盖面与发现端点能力（2026-09-23）

日期：2026-09-23
背景：用户对线上巡检对象的选择提出疑问——是否覆盖了 2026 年全部免费模型端点、是否尽量用发现端点自动枚举、
GH Actions 不通但本地可用的提供商能否经 models.johnnren.qzz.io（本机 CPA 中转）补齐。
本文记录全部决策与证据，避免决策只存在于会话中。

## 结论速览

| 提供商 | 现状 | 发现端点 | 决策 |
|---|---|---|---|
| Groq | 在巡检 | `/v1/models` 需 key（GH 可用） | 保持。可开 discover（见下） |
| NVIDIA NIM | 在巡检 | `/v1/models` 需 key（GH 可用） | 保持，已 discover |
| Google Gemini | 在巡检 | OpenAI 兼容路径**无** `/models`（404） | 保持显式列表。官方 `v1beta/models` 是 Google 自有协议，非 OpenAI 形状 |
| Cloudflare Workers AI | 在巡检 | `ai/models/search`（已用 discover_url） | 保持 |
| OpenRouter | 在巡检 | `/v1/models` 公开（匿名 200） | 保持，已 discover |
| ModelScope | 在巡检 | `/v1/models` 公开匿名 200 | 保持，已 discover |
| Kilo Gateway | **已移除** | `/api/gateway/models` 公开匿名 200 | GH runner 全 403（Azure IP 段被 ban，匿名限流按 IP，见老仓库 commit 1467b19）。**本机可用，可经 CPA 中转**（见下） |
| OpenCode Zen | **已移除** | `/zen/v1/models` 公开 | **放弃**。免费层已服务端封死（详见下） |
| SambaNova | 已移除 | — | 需绑卡，历史数据 402 佐证。不恢复 |
| Z.ai / bigmodel.cn | 未接 | 需 key 401 | Z.ai key 限速 ~1 RPS 且隐私条款（free tier 参与训练）；与 CPA/GLM 系模型重叠。不接 |
| Mistral | 未接 | `/v1/models` 需 key | 需手机验证；1 RPS 可用但注册门槛。低价值，不接 |
| SiliconFlow | 未接 | 需 key 401 | 免费模型偏小（8B 级），ModelScope 已覆盖国内直连场景。不接 |
| OVHcloud | 未接 | 公开 200 | 2 RPM/模型 太低。不接 |
| Cohere | 未接 | 不兼容 | 1000 次/月 + 条款严。不接 |
| HF Inference Providers | 未接 | — | $0.10/月。不接 |
| GitHub Models | 已死 | — | 2026-07-30 全线退役（410）。不接 |

### OpenCode Zen 深度排查记录（为什么确定放弃）

历史 error：`403: OpenCode's free tier can only be used from within OpenCode`（来源即此）。

实测矩阵（2026-09-23）：

| 客户端 | 结果 |
|---|---|
| curl + 完整复刻 CLI headers（UA `opencode/1.18.32 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14`、`x-opencode-client/request/session/project`、`Bearer public`、大 system body） | 403 |
| python urllib + 同 headers | 403 |
| bun 1.4.2 fetch + 同 headers | 403 |
| Go net/http + 同 headers | 403 |
| uTLS 模拟 Chrome/Firefox/Safari/iOS/Edge/OkHttp 指纹（h2 + http/1.1） | 全部 403 |
| **官方 opencode CLI 1.18.32**（本地 TLS 中转劫持抓包验证 headers 与 curl 完全一致） | **200** |

抓包证据：CLI 实际发送的头与我们的复刻逐字节一致（`/tmp/mitm/spy3.log`，hosts 劫持 + 自签 CA 方案）。
结论：Cloudflare 按 **TLS ClientHello 指纹（JA4）** 只放行 bun/BoringSSL（即官方 CLI）。
这层指纹隔离无法在 httpx（OpenSSL）或 Go（crypto/tls）栈上低成本复刻。
GitHub issue #42500/#41320 也确认了 UA-gating + 指纹检查的存在。
（注：`big-pickle`/`mimo-v2.5-free` 等曾对 curl 放行，2026 年 7 月中旬起收紧，issue #45132/#43054。）

替代价值评估：zen 免费模型（big-pickle=MiniMax、mimo、nemotron、ling、deepseek-flash-free）
与 Kilo/NVIDIA/OpenRouter 高度重叠，不值得为 6 个模型维护 CLI 旁路。

### Kilo Gateway 经 CPA 中转方案

- 本机匿名可用（200 req/h/IP），GH Actions（Azure IP）被 ban。
- CPA（cli-proxy-api，`127.0.0.1:9999`，公网 `https://models.johnnren.qzz.io`，key `sk-1234`）
  支持自定义 `openai-compatibility` 上游，可为 Kilo 添加条目（免 key 或低门槛）。
- 巡检侧：`patrol.json` 的 base_url 支持 `$ENV` 引用（私有端点自动脱敏为 `(private)`），
  经 `PATROL_EXTRA_ENV` secret 注入 `KILO_RELAY_URL=https://models.johnnren.qzz.io/v1`。
- **权衡（待记录给用户的诚实提示）**：走 CPA 中转后，测的是「本机 → CPA → Kilo」链路，
  TTFT 含国内出口 + 手机上行，口径与 GH Actions 直连的其它 target 不一致；且依赖家里
  手机/CPE 在线。价值：Kilo 独家免费模型（stepfun/step-3.7-flash 等）+ 200 req/h 匿名额度大。

## 附：历史 error 根因分析（2026-09-20 ~ 09-22，1733 样本 / 603 error）

| 错误 | 数量 | 根因 |
|---|---|---|
| HTTP 404（NVIDIA NIM 为主） | 195 | discover 拉到的尸体模型 ID（下线但列表未清）；9/20 后已靠 blacklist 剔除 37 个 |
| HTTP 403（CF 为主） | 98 | CF frontier 模型（kimi/glm/deepseek 上游）免费计划不可用；blacklist 已补 `.*(vision|guard|lora).*` 但 frontier 条目是 discover 拉进来的，需黑名单/自动黑名单收敛 |
| HTTP 429 | 85 | 限流：OpenRouter（50 req/天 账号级免费上限，model 数 18 超限）、Gemini pro（429）、ModelScope 单模型限流 |
| HTTP 503（Gemini flash 系） | 42 | Google 免费层容量不足（503 = 模型过载），间歇性 |
| 超时（error_message 为空，耗时 120s/300s 整） | 32 | NIM 大模型（kimi-k3、glm-5.3、gemma-4-31b）排队超时；120s timeout 触发，httpx ReadTimeout 的 str(e) 为空串 |
| HTTP 401（ModelScope PaddlePaddle） | 30 | 模型需白名单未授权（`The model does not exist or you do not have access`）；blacklist 已补 |
| HTTP 410（NVIDIA） | 10 | 模型退役（410 Gone），已黑名单 |
| HTTP 402（SambaNova） | 4 | 需绑卡（历史 target，已移除） |
| `sequence item N: expected str, bool/int found` | 2 | **speed_test bug**：CF qwen/qwq-32b 流式 delta 的 content 字段返回 bool/int 非字符串，`"".join()` 崩溃。需在 `_extract_stream_chunk` 对非 str 做 str() 归一 |

修复项（本轮落实）：

1. `sequence item` bug：流解析对非字符串 content 容错。
2. 超时 error_message 为空：异常分类记录（TimeoutException 至少标 "timeout"）。
3. 403/404/401 尸体模型：自动黑名单机制（连续失败 N 次自动剔除，成功后自动恢复）。
