# 检视报告 — 发布前整体检视（260923-provider-coverage prerelease）

## 概要

对全仓库做了公开发布视角的整体检视：fork 快速开始路径、README 与代码一致性、看板无数据/少数据表现、敏感信息泄露风险，以及 commit 范围 `6bf9f61..0413051` 涉及的全部文件。整体架构清晰、README 与实现高度一致、密钥治理做得好；但发现 1 个正在污染公开示例看板的阻塞问题（Groq 动态发现拉入展示名模型导致大量 404 噪音）。

## 需求对齐

近期四项工作均已正确落地，README 与代码/配置逐项核对一致：

- 巡检周期：`patrol.yml` cron `13 */2 * * *`，与 README「默认每 2 小时」一致。
- 数据窗口：看板 `MAX_DAYS = 14` + 14d 档懒加载（`PRELOAD_DAYS = 7`），workflow prune `-mtime +14`，与 README「最多 14 天」一致。
- tooltip 上移 12px、生成参数 hover：均已实现且 `escapeHtml` 转义到位。
- Groq/Gemini discover + 黑名单、NIM 黑名单精简：已生效（最新一轮 Gemini 15 个 chat 模型、NIM 15 个，过滤正确）。
- 测速口径章节（TTFT/content TTFT/TPS/ITL/思考时长/`_reconcile_token_counts`）与 `speed_test.py` 实现逐条对得上。
- 敏感信息：密钥只存环境变量名，结果 JSONL 经 grep 无任何 key/token 泄露，测试也断言了脱敏。

但 **Groq 的 discover 改动带来了一个次生问题**，见阻塞问题 B1。

## 阻塞问题

| ID | 位置 | 问题 | 建议 |
| --- | ---- | ---- | ---- |
| B1 | `backend/patrol_runner.py:87` | `discover_models` 取 `m.get("name") or m.get("id")`（为兼容 Cloudflare 的 UUID id 而优先 name），但 Groq 的 `/models` 响应同样带 `name` 字段且值是**展示名**。证据：最新一轮（2026-09-23T00:25）Groq 16 个测试中 10 个是展示名模型（"GPT OSS 20B"、"Whisper Large V3 Turbo"、"Prompt Guard 2 86M" 等），全部 404；黑名单 `.*(whisper\|orpheus\|prompt-guard).*` 大小写不匹配拦不住展示名。后果：(1) 公开示例看板（README 门面）Groq 成功率 ~37%，噪音行直接可见；(2) 展示名与真实 id 形成重复行（`qwen/qwen3.8-27b` 与 `Qwen/Qwen3.8-27B`）；(3) 这些条目会自动拉黑自愈（约 5 轮后），但每次上游新增模型都会重复这个过程，blacklist.json 持续积垃圾。 | 优先取 `id`，仅当 `id` 缺失或形如 UUID（可用 `re` 判 `[0-9a-f]{8}-...`）时回退 `name`——Cloudflare 兼容不破坏，Groq 恢复正常。同时建议 `filter_models` 编译 regex 加 `re.IGNORECASE` 作为兜底。修复后观察一轮确认 Groq 噪音行消失。 |

## 建议修改

| ID | 位置 | 问题 | 建议 |
| --- | ---- | ---- | ---- |
| S1 | `.github/workflows/patrol.yml:46` | prune 步骤 commit 消息写 "prune data older than 7 days"，实际删除的是 >14 天的文件。git log 会持续产生误导。 | 消息改为 `patrol: prune data older than 14 days`。 |
| S2 | `README.md:16-31` | 快速开始顺序问题：第 5 步触发首次巡逻时 Pages 尚未开启（第 6 步才开），`workflow_run` 联动的 deploy 会因 Pages 未初始化而失败；用户开完 Pages 后需再等 2 小时下一轮巡检才能看到数据，首体验断裂。 | 把「开启 Pages」提到「触发首次巡逻」之前；或在第 6 步补一句「开启后手动 Run 一次 Deploy website workflow（或等下一轮巡检）」。 |
| S3 | `README.md` 快速开始 | fork 用户若不配置默认 6 个端点的密钥（Secret 里没有 GROQ_API_KEY 等），整轮 401，5 轮后全部被自动拉黑，看板全空且 blacklist.json 全是尸体。README 未提示。 | 快速开始补一句：不使用默认免费端点的，请删除 `patrol.json` 中对应 targets，未配密钥的端点会全部失败并自动拉黑。 |
| S4 | `backend/patrol_runner.py:170-177` | 私有 URL 脱敏用 `resolve_base_url()` 的原始返回值做 map key，而实际请求/异常消息里的 URL 是 `normalize_base_url()` 处理过的（rstrip `/`）。env 值带尾斜杠时（如 `https://x.com/v1/`）替换不命中，私有 URL 会泄漏进公开 JSONL。 | `private_map` 同时收录原始值与 normalize 后的值两个 key。 |
| S5 | `backend/patrol_runner.py:48-70` | `blacklist.json` 无 GC：模型从上游消失后（尤其 B1 这类误入展示名被拉黑的），条目永久残留，长期运行文件无限膨胀。 | `update_auto_blacklist` 顺带剔除「本轮 tests 中已不存在且无 blacklisted_at」或「连续 N 轮未出现」的条目。 |
| S6 | `backend/patrol_runner.py:184-196` | JSONL 每行含完整 `response_content`（实测约占体积 1/3），看板从不读取；7 天预载约 1.9MB，巡检更频繁的 fork 用户会更大。生成的完整内容也随公开仓库发布。 | 落盘前剔除或截断（如保留前 100 字符）`response_content`；生成参数看板已通过 `prompt`/`max_tokens`/`temperature` 字段单独展示，不损失信息。 |
| S7 | `config/patrol.json:177` | `_comment` 称 Kilo Relay「启用方法见 README」，但 README 没有对应章节；注释还暴露了私有中转域名。 | 在 README 补 Kilo Relay 启用小节，或删掉注释中的域名与「见 README」表述。 |
| S8 | `config/patrol.json:8` + Groq 限额 | `max_tokens: 1024` 超过 Groq `qwen/qwen3.8-27b` 的 OTPM 1000 限制（实测 429: "Limit 1000, Requested 1024"），该模型每轮都有概率 429，累计 5 次会被误拉黑。 | 将 `max_tokens` 降到 1000 以下（如 768，对测速口径无实质影响），或在 README 免费端点表 Groq 行标注此坑。 |

## 非阻塞问题

| ID | 位置 | 问题 | 建议 |
| --- | ---- | ---- | ---- |
| N1 | `AGENTS.md:16` | 仍写「巡检 cron（每6h）」，实际已是每 2 小时。 | 顺手更新。 |
| N2 | `.github/workflows/patrol.yml:57` | push/rebase 硬编码 `origin master`，fork 用户若把默认分支改名（main）会持续 push 失败。 | 可用 `${{ github.ref_name }}` 或 README 提一句「保持 master 分支名」。 |
| N3 | `config/patrol.json:101,105` | Cloudflare account ID 明文出现在 base_url/discover_url。非密钥（不可单独利用），但属可避免的个人信息暴露。 | 可走 `$ENV` 引用（机制已支持）；作为公开示范保留也可接受，标注备忘。 |
| N4 | `website/index.html:1059` | 空数据文案「Fork 仓库并启用 Patrol workflow 后，数据会在此自动展示」是写给本仓库访客的；fork 所有者自己的看板在等首轮数据时看到这句话语义不通。 | 文案改为中性表述，如「暂无巡检数据，等待首轮巡检完成或检查 Patrol workflow 是否启用」。 |
| N5 | `website/index.html:751` | 时间档 chips 只影响表格与 sparkline，趋势图始终画全部已加载数据；选「最近一次/24h」时图表不随之收缩，交互预期可能不一致。 | 明示行为（图表旁标注当前窗口）或让图表跟随时间档过滤。 |
| N6 | `website/index.html:652` | `col === 'model' || col === 'provider'` 中 provider 分支是死代码（表格无 provider 排序列）。 | 顺手清理。 |
| N7 | `website/index.html:358` | `fetch(f + '?t=' + Date.now())` 使所有 JSONL 永不缓存，每次访问全量拉 ~1.9MB。 | 只对当天文件加 bust，历史文件允许缓存（内容不可变）。 |

## 准入结论

**结论**：`不准入`

**说明**：B1 正在实时污染 README 引以为门面的公开示例看板（Groq 62% 失败、噪音行可见），修复成本极低（discover 字段优先级一行改动），应在发布帖发出前修复并观察一轮巡检确认。其余建议项不阻塞，可随发布后的迭代处理（S1/S2/S3 建议趁 README 还在手上时顺手改掉，fork 用户首体验相关）。
