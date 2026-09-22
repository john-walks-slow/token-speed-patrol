# 调研：免费提供商政策与巡检频率/RPS 建议（2026-09-23）

日期：2026-09-23
关联：[260923-provider-coverage.research.md](260923-provider-coverage.research.md)（覆盖面决策）

## 一、各提供商免费政策（与巡检相关）

| 提供商 | 免费限额 | 每日巡检消耗（当前 97 模型口径） | 风险评估 |
|---|---|---|---|
| Groq | 每模型 30 RPM / 1K RPD / 8K TPM / 200K TPD | 7 模型：6h→28，1h→168 | 无风险 |
| NVIDIA NIM | 免费层 ~40 RPM（账号级） | 11 模型：6h→44，1h→264 | 无风险（max_rpm=20 已限） |
| Google Gemini (AI Studio key) | free 层按模型：flash 系约 10-15 RPM、1500 RPD 级；pro 系约 2-5 RPM、**约 50-100 RPD**；RPD 太平洋时间午夜重置 | 12 模型：6h→48，1h→288 | **pro-latest 是瓶颈**：2 个 pro 模型 1h/次=48 RPD，贴 50 RPD 上限。历史 429 全在 pro-latest 上（17 次），佐证 |
| Cloudflare Workers AI | 10,000 Neurons/天；文本 300 RPM；frontier 模型 403（免费计划不可用） | 25 模型：6h→100，1h→600 req；Neurons 消耗：每次 ~20 输入+256 输出 token，粗估 0.6-2 neuron/token 级别 → 每日 1h/次约 60K-150K 输出 token/天，可能触顶 | **Neurons 是硬顶**：小模型 1h/次可行，整体建议保留观察 |
| OpenRouter | 20 RPM；free 50 RPD（账号级，UTC 午夜重置）；曾购 $10 → 1000 RPD | 18 模型：6h→72，1h→432 | 实测：4 runs/天 全程成功 → 用户 key 为 1000 RPD 档。1h/次 = 432 RPD，仍在额度内。429 全是模型级（gemma/qwen/glm 上游容量），非账号配额 |
| ModelScope | 2000 次/天（全模型共享，0 点重置）；主力模型约 500 次/天/模型 + 动态 QPS | 24 模型：6h→96，1h→576 | 无风险（总额度 576/2000）。429 为单模型过载 + QPS 突升（并发=1 已缓解） |
| Kilo（经 CPA 中转） | 匿名 200 req/h/IP | 13 模型：1h→13 req/h | 无风险（且 CPA 侧还有别在跑的流量，但量级小） |

## 二、频率建议

**结论：1h/次可行，但 Gemini pro 与 Cloudflare Neurons 需要兜底。**

| 方案 | 评价 |
|---|---|
| 1h/次（24 runs/天） | ✅ 推荐。总量 ~110 模型 × 24 = ~2600 req/天。除 Gemini pro（贴线）与 CF Neurons（需观察）外全部有余量 |
| 6h/次（现状） | 数据点太稀，sparkline/趋势图没意义 |
| 30min/次 | Gemini pro 必超；OpenRouter 1000 RPD 贴线。不推荐 |

配套调整：

1. **Gemini 拆分巡检频率**：flash 系 1h；pro-latest 两个模型保持 6h（或对 pro 目标单独 `max_rpm` 与独立 target）。最简单做法：把 `gemini-pro-latest`/`gemini-3.1-pro-preview` 移到独立低频 target。
2. **max_rpm：全局 20 → 保持 20**。各 provider 分桶限流互不影响；20 RPM 对 1h/次的量级绰绰有余（110 模型 5.5 分钟跑完一轮）。
3. **concurrency=1 保持**：ModelScope 的 "Request rate increased too quickly" 需要。
4. **iterations 保持 1**：多轮会让所有配额翻倍，收益低。
5. **max_tokens 256 → 128（可选）**：巡检目的是测 TTFT/TPS 不是看生成质量，128 token 足够拉出稳定 ITL 样本，同时把 CF Neurons 与 TPM 消耗减半。本轮不动（保持口径连续性）。

## 三、GitHub Actions 层面

- cron `13 */6 * * *` → `13 * * * *`（每小时，13 分错峰）。Actions 公开仓库免费额度充足（每小时一次远低于限制）。
- 单 run ~5.5 分钟 + commit/push，不会与下一轮撞车。
- 每 run 数据量：~110 模型 × ~1KB/条 ≈ 110KB JSONL/行。一天 24 行 ≈ 2.6MB。看板按天加载、sparkline 读 30 天 = 每天文件变大后需注意看板加载性能（当前 index.html 已有截断逻辑则无碍，提交后观察）。

## 四、数据佐证（历史 error 已回答"为什么 error"）

- Gemini 429：全部集中在 `gemini-pro-latest`/`gemini-3.1-pro-preview`（17 次）→ free 层 pro RPD 低。
- Gemini 503：flash 系（`3.7/3.8-flash`、`flash-latest`）过载，间歇性，非配额。
- OpenRouter 429：模型级（gemma/qwen/glm/poolside 上游容量），同 run 其他模型成功 → 非账号配额。
- ModelScope 429：单模型过载（GLM-4.7-Flash）+ QPS 突升（并发=1 下仍有）。
- NIM 空错误消息（32 次）：120s/300s 整耗时 → ReadTimeout（httpx 超时异常 str 为空）。
- CF 403：frontier 模型（kimi/glm/deepseek-v4-pro）免费计划不可用 → 自动黑名单候选。
- `sequence item N: expected str`：speed_test 流解析 bug（CF qwen delta 返回非 str content）→ 修复项。
