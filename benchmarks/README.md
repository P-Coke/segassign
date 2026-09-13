# 基准与等价性记录

本目录保留 2026-08-13 的历史实验结果，对应更名前的 GeoToolBox v2。数据集和商业 eCognition SDK 不随仓库发布。数值用于记录当时的实验条件，不代表不同硬件、影像或后续版本的速度保证。

## 已记录的观测

`results/production-performance-20260813.json` 中，完整冷运行由 v1 的 3181.625 秒降至 v2 的约 1903.792 秒（含后续清理增量），耗时减少约 40.16%。同次任务赋值阶段约快 4.78 倍。

“排除 eCognition 后约 2.18 倍”的指标使用总墙钟时间减去累计引擎 tile 时间除以并发数的估算残差，并非对并行流水线非引擎时间的直接独立测量。同报告中保守的具名阶段比较为约 1.85 倍。

`results/production-attributes-20260813.json` 记录了 180591 个要素的几何一致性。默认确定性排序与捕获的 v1 运行在 49 个接缝要素属性上不同；按当次 v1 顺序重放后属性差异为零。原因是 v1 曾将 worker 完成顺序用于栅格化重叠优先级，因此不能把这些记录描述为默认输出字段逐项严格相同。

后续赋值模块升级的独立测试见 `results/production-zonal-20260813.json`。不要将不同版本/运行的最快数值拼接为同一套实验结果。

## 运行方法

在仓库根目录使用已有环境：

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
mamba run -n Geo-Workspace python benchmarks/run_micro.py --repeats 5 --output .benchmarks/micro.json
```

添加 `--enforce` 可启用计数和候选查询分别至少 3 倍的微基准门禁。完整任务见 `run_production.py --help`，需要自行提供输入和已授权的 SDK。

冷运行使用独立输出及空工作目录，暖运行与断点恢复分开统计。新结果写入 `.benchmarks/`，不要覆盖本目录已归档的历史观测。
