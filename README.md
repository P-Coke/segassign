# SegAssign

遥感科研项目配套的影像分割与矢量赋值自动化工具。

SegAssign 将 eCognition 影像分割与分类栅格向矢量属性赋值串联起来，减少重复操作，支持 00/01/02 全流程文件夹批处理、结构化运行报告和断点续跑。

## 功能

| 入口 | 输入 | 输出 |
| --- | --- | --- |
| 00 分割并赋值 | 亚米级影像、2 米基础影像、分类影像（均支持单文件或文件夹） | 单个 `.shp` 或输出目录（保留相对结构） |
| 01 单独分割 | 亚米级影像、2 米基础影像（均支持单文件或文件夹） | 单个 `.shp` 或输出目录（保留相对结构） |
| 02 单独赋值 | 单个 Shapefile 或矢量文件夹、分类栅格 | 指定目录内的赋值结果 |

- 00/01 批量处理时：基础影像和分类影像选择单文件则全局共用；选择文件夹时优先按相对路径与同名匹配，其次按空间重叠面积自动匹配。
- 02 文件夹赋值可递归发现 `.shp`，保留原文件名和相对目录结构。
- 使用 `class/v1/v1p...` 表达类别及像元占比；不输出 `tile_id/source_fid/gap_fill/seam_band` 内部字段。
- TIFF 读取失败会重新打开文件，最多共尝试 3 次；持续失败会报告影像、tile 和读取窗口。
- 输入不复制、不覆盖；工作缓存与最终成果分开保存。

## Windows 使用

02 赋值不需要 eCognition。00/01 分割需要本机已安装并授权的 **eCognition Developer 9**，在界面选择安装目录；SDK 和许可证不随本项目分发。

批处理输出目录应与所有输入目录分开，不允许放入输入目录或其子目录内。

### 分区域二次赋值

区域 A 的输出可作为区域 B 的输入。务必保留同名 `.gtz.npz` 计数文件及 Shapefile 全部伴随文件；同一个跨区面将按 A+B 的原始像元计数重新计算占比。

此方式要求区域分类编码和像元面积一致、有效区域不重叠。当前不会对重复输入或重叠区域去重，不要在两次赋值之间修改几何或重排要素。

### 硬件自适应调优与缓存策略

- 启动时自动识别物理核/逻辑线程、实时可用内存及本机最快空闲内部 SSD（优先 NVMe/SATA 非系统盘），在“自动最优”模式下匹配并发数与小图自适应瓦片尺寸。
- 运行中每块瓦片完成 eCognition 与后处理后即时释放原始输入与 `ObjectShapes.shp`；默认勾选“完成后自动清理缓存”，任务成功后自动清除全部临时缓存仅归档极小运行报告。
- 任务中断、取消或失败时自动保留断点以便“断点续跑”；超过 7 天未使用的历史残留缓存将在启动时自动回收，也可在界面一键手动清理。

## 开发与验证

优先使用已有 `Geo-Workspace`：

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
mamba run -n Geo-Workspace python -m segassign.ui
mamba run -n Geo-Workspace python -m pytest
mamba run -n Geo-Workspace python -m ruff check src tests benchmarks packaging
mamba run -n Geo-Workspace python benchmarks/run_micro.py --repeats 5 --enforce
```

构建 Windows 便携包：

```powershell
.\packaging\build_release.ps1 -Version 2.1.0
```

## 项目结构

```text
src/segassign/
  models.py           请求、计划、事件和结果模型
  raster_batch.py     GeoTIFF 批发现与多目录自动配对
  runtime/            硬件探测、资源调度、取消、报告、生命周期缓存与输出事务
  segment/            影像准备、eCognition、后处理、拼接和批处理
  zonal/              空间候选查询、栅格计数、赋值和批处理
  composite.py        编排 segment -> zonal 及批量执行
  ui/                 PySide6 响应式桌面界面及后台任务适配
assets/               规则集、已校验的内部 bridge 和哈希清单
tests/                单元测试与回归测试
benchmarks/           核心热点微基准
packaging/            Windows 便携包构建配置
```

Python 公共入口为 `segassign.segment`、`segassign.zonal`、`segassign.composite`，均提供 `plan`/`run` 和 `plan_batch`/`run_batch`。

## 许可证

项目代码采用 [MIT License](LICENSE)。eCognition 和其他第三方组件仍适用各自许可；依赖与内部 bridge 说明见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。
