# 微基准说明

`run_micro.py` 用于本地回归验证分类赋值核心热点（`bincount_rows` 计数与 `CandidateIndex` 空间索引）的吞吐性能。

## 运行方法

在仓库根目录使用 `Geo-Workspace` 环境：

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
mamba run -n Geo-Workspace python benchmarks/run_micro.py --repeats 5 --enforce
```

`--enforce` 要求计数与空间候选查询相对基线参考实现均达到至少 3 倍加速。
