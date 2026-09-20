# 巡检结果目录

此目录存放 GitHub Actions 巡检（`.github/workflows/patrol.yml`）产出的 JSONL 结果文件。

文件命名：`YYYY-MM-DD.jsonl`，每行一个 run 的完整结果数组。

静态看板 `../index.html` 通过 fetch 读取本目录下的 JSONL 文件渲染趋势。
