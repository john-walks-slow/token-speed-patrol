# 计划：自动黑名单 + 巡检频率最佳配置

日期：2026-09-23
关联：[260923-provider-coverage.research.md](260923-provider-coverage.research.md)、[260923-rate-limit-policy.research.md](260923-rate-limit-policy.research.md)

## 需求

1. 连续失败的模型自动黑名单，不再纳入巡检（用户明确要求）
2. 全部完成后应用最佳频率（1h）与 RPS 配置并提交

## 设计

### 自动黑名单

**存储**：`website/data/blacklist.json`，随巡检结果一起 commit 进仓库。理由：
- GH Actions 是无状态 runner，仓库即唯一持久层（与 JSONL 数据同构）
- 公开透明：看板/用户可直接看到谁被拉黑、为什么
- fork 用户免配置，开箱即用

**结构**：

```json
{
  "failures": {
    "provider_name/model_id": {
      "consecutive_failures": 3,
      "last_error": "HTTP 403: ...",
      "blacklisted_at": "2026-09-23T..." (仅已拉黑时有)
    }
  }
}
```

**规则**（写入 `patrol_runner.py`，巡检结束时更新）：
- 每轮巡检后，按 (provider_name, model) 归组统计本轮成败
- 失败 → `consecutive_failures += 1`；成功 → 清零并删除记录（自动恢复机制）
- `consecutive_failures >= N`（默认 5）→ 加入黑名单：下一轮起被 `to_tests` 过滤
- 黑名单命中错误类别白名单才计数？——**不区分错误类别**：429 限流间歇失败通常不会连续 5 次；
  503 过载同理。真连续 5 次失败的就是尸体（403 权限/404 下线/401 无权限）或彻底超时。
  简单规则 > 精细规则（用户规范：不过度防御）。
- 429 不计入：**修正**——连续 5 次 429 也该剔除（如 OpenRouter gemma 系每日 429）。
  保持不区分。

**过滤点**：`patrol_config.PatrolTarget.filter_models` 已有 blacklist（regex）。
自动黑名单在 runner 层做集合差（不污染 regex 语义）：
`tests = [t for t in tests if (provider, model) not in auto_blacklist]`

**新 discover 回来的模型**天然不在 failures 字典里，不受影响——黑名单只挡"已知失败者"。

**手动恢复**：删除 blacklist.json 对应条目或整个文件即可（README 注明）。

### 配置变更（patrol.json）

- workflow cron：`13 */6 * * *` → `13 * * * *`（每小时）
- Gemini 拆分：pro 系（`gemini-pro-latest`、`gemini-3.1-pro-preview`）留独立 target，
  与 flash 系分开——但 PatrolConfig 无 per-target cron 概念，巡检是单次全量。
  **简化**：pro 系留在原 target（1h/次 × 2 模型 = 48 RPD，贴 50 RPD 但在额度内；
  若连续 429 会被自动黑名单临时压制，成功即恢复）。可接受。
- max_rpm 20 保持；concurrency 1 保持；timeout 120 保持
- 新增 Kilo（经 CPA 中转）target：base_url `$KILO_RELAY_URL`，key `$KILO_RELAY_KEY`，
  经 PATROL_EXTRA_ENV 注入。私有端点自动脱敏为 (private)。
  注：该 target 走家里网络，口径与 GH 直连不同——文档中如实记录。
  **决定：默认 disabled 由用户在 GH Secrets 配置后启用**——加 `enabled: false` 字段太复杂，
  直接写入 config，用户填 PATROL_EXTRA_ENV 即激活；未填时 base_url 解析为空串、
  全部请求失败 → 连续失败进黑名单。为避免无 secret 时污染数据，**用 `models: []` + 不开 discover**，
  并在 README 注明启用步骤。不行——空 models 会被 runner 跳过（no targets → 该 provider 0 test），
  这正是想要的行为：未配置则静默跳过。等等，tests 为空列表时该 target 展开为 0 条，
  不影响其他 provider。可行：**Kilo target 默认 models 为空且 discover=false，用户按 README
  填 models 后生效**。

### 顺手修复（已完成于本计划）

1. `_extract_stream_chunk` 对非字符串 content 容错（CF qwen bug）
2. 超时异常空消息用类名兜底（`ReadTimeout`）

## 实施步骤

1. ✅ speed_test 两个 bug 修复
2. patrol_runner：巡检后更新 blacklist.json（读旧 → 更新 → 过滤 → 写回）
3. patrol.json：加 Kilo target（空 models 占位）；频率在 workflow 改 cron
4. 测试：test_patrol.py 补自动黑名单用例 + 同步 patrol.json 断言
5. README：secrets 表补 Kilo 中转说明 + blacklist.json 说明
6. 全量 pytest → reviewer 检视 → commit（分两个 commit：fix + feat）
