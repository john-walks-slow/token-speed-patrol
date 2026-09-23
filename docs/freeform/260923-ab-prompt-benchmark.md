# 测速 Prompt A/B 实测：故事生成 vs 固定文本复述

日期：2026-09-23。起因：评估"让模型复述一段固定文本"（llmperf 式）是否应替换现行故事生成 prompt。

## 方法

- 用仓库测速内核 `execute_batch_tests`，同一批 9 个模型（Groq / NVIDIA NIM / Gemini 各 3 个），两种 prompt 各 3 轮取均值。
- Prompt A（现行）：`Hello, tell me a short story in 3 sentences.`
- Prompt B（复述）：让模型逐字复述一段约 90 词的固定英文文本（llmperf 风格：复述 + 不许提前收尾）。
- max_tokens 1024、流式、concurrency 1，经本机代理实测。

## 结果

| 模型 | TPS A | TPS B | diff | TTFT | ITL |
|---|---|---|---|---|---|
| Groq / gpt-oss-20b | 71.5 | 134.7 | +88% | +63% | +1351%（B 更长） |
| Groq / qwen3.8-27b | 27.5 | 40.4 | +47% | −4% | −11% |
| Groq / allam-2-7b | 53.8 | 45.3 | −16% | +10% | −71% |
| NIM / nemotron-3-super-120b | 56.8 | 82.6 | +46% | −45% | −60% |
| NIM / deepseek-v4.1-flash | 7.9 | 15.7 | +99% | +103% | −67% |
| NIM / diffusiongemma-26b | 11.3 | 36.3 | +220% | −55% | —（扩散模型 ITL≈0 无意义） |
| Gemini / 3.8-flash | 11.9 | 12.2 | +2% | +23% | −65% |
| Gemini / 3.5-flash-lite | 17.0 | 30.5 | +80% | −29% | −45% |
| Gemini / gemma-4-26b | 2.2 | 7.0 | +216% | −7% | −73% |

## 结论

1. **两种 prompt 测出来的 TPS 不是同一个东西**：复述是逐字复制，无生成推理负担，9 个模型 8 个变快（最多 3 倍）。故事生成更贴近真实使用。
2. TTFT 方向不一致——首字延迟主要取决于排队/思考，与 prompt 内容关系不大。
3. 复述 prompt 的副作用：Groq qwen3.8 报 `429 Request too large`（输入 token 变长撞 TPM 限制）；部分小模型可能不配合复述指令。
4. **决策：保持故事 prompt 不变**。看板定位是"哪个模型用起来快"，故事口径更保守但更诚实；复述口径是"复读机吞吐上限"，会高估真实用途。

## 参照

- llmperf：默认 prompt 即复述莎士比亚 + don't-stop，目的是跨厂商公平对比 + 输出长度可控。
- Artificial Analysis：反其道用 60 条多样化真实 prompt，更贴近真实场景。
