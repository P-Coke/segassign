# SegAssign

遥感科研项目配套的影像分割与矢量赋值自动化工具。

SegAssign 将 eCognition 影像分割与分类栅格向矢量属性赋值串联起来，减少重复操作，支持文件夹批处理、结构化运行报告和断点续跑。它是研究数据处理流程的辅助程序。

## 功能

| 入口 | 输入 | 输出 |
| --- | --- | --- |
| 00 分割并赋值 | 夏季影像、冬季影像、分类栅格 | 赋值后的 Shapefile |
| 01 单独分割 | 夏季影像、冬季影像 | 分割面 Shapefile |
| 02 单独赋值 | 单个 Shapefile 或矢量文件夹、分类栅格 | 指定目录内的赋值结果 |

- 文件夹赋值可递归发现 `.shp`，保留原文件名和相对目录结构。
- 使用 `class/v1/v1p...` 表达类别及像元占比；不输出 `tile_id/source_fid/gap_fill` 内部字段。
- TIFF 读取失败会重新打开文件，最多共尝试 3 次；持续失败会报告影像、tile 和读取窗口。
- 输入不复制、不覆盖；工作缓存与最终成果分开保存。

## Windows 使用

从 [GitHub Releases](https://github.com/P-Coke/segassign/releases/latest) 下载 `SegAssign-v2.0.2-Windows-x64.zip`，完整解压后运行 `SegAssign.exe`。请保留同目录的 `_internal` 文件夹。

02 赋值不需要 eCognition。00/01 分割需要本机已安装并授权的 **eCognition Developer 9**，在界面选择安装目录；SDK 和许可证不随本项目分发。

输出目录应与矢量输入目录分开；文件夹批处理不允许把输出放入输入目录或其子目录，避免重复发现结果。

### 分区域二次赋值

区域 A 的输出可作为区域 B 的输入。务必保留同名 `.gtz.npz` 计数文件及 Shapefile 全部伴随文件；同一个跨区面将按 A+B 的原始像元计数重新计算占比。

此方式要求区域分类编码和像元面积一致、有效区域不重叠。当前不会对重复输入或重叠区域去重，重复处理会重复计数。不要在两次赋值之间修改几何或重排要素。没有计数文件的旧成果无法还原原始统计量。

当前栅格没有有效覆盖且此前也未计数的要素保留已有属性；首次赋值无统计时不强制填 0。

### 中断与续跑

工作目录由程序自动管理，名称保留为 `.geotoolbox-work`，以沿用已有运行目录约定。保留该目录，使用相同输入、程序/规则集路径和参数，勾选“断点续跑”再次运行。输入指纹变化会使相关缓存失效。

瞬时读取异常和部分 worker 故障可以自动重试；持续失败会停止并生成报告。程序退出或断电后需要人工重新启动，不会自动重启。文件缺失或损坏不能通过重试修复，也不会把读取失败区域作为 0 或无效区跳过。

## 开发与验证

优先使用已有 `Geo-Workspace`，不需要 pip 或项目 `.venv`。新工作站可用环境文件准备依赖：

```powershell
mamba env create -f environment.yml
```

从仓库根目录启动 UI、测试及检查：

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
mamba run -n Geo-Workspace python -m segassign.ui
mamba run -n Geo-Workspace python -m pytest
mamba run -n Geo-Workspace python -m ruff check src tests benchmarks packaging
```

默认测试使用生成的小样本，排除需要商业 SDK 或完整业务数据的测试；不代表所有客户数据已验证。

构建 Windows 便携包：

```powershell
.\packaging\build_release.ps1 -Version 2.0.2
```

产物位于 `release/v2.0.2/`。源码仓库不包含环境、业务数据、工作缓存及生成的发布包。

## 项目结构

```text
src/segassign/
  api.py, models.py    公共 API、请求、结果和事件类型
  runtime/            资源调度、取消、报告、断点和输出事务
  segment/            影像准备、eCognition、后处理和拼接
  zonal/              空间候选查询、栅格计数、赋值和批处理
  composite.py        只编排 segment → zonal
  ui/                 PySide6 界面及后台任务适配
assets/               规则集、已校验的内部 bridge 和哈希清单
tests/                小样本回归测试
benchmarks/           基准脚本及历史测量记录
packaging/            单一 EXE 的构建配置
```

Python 调用入口为 `segassign.segment`、`segassign.zonal`、`segassign.composite`，均提供 `plan(request)` 和 `run(request, context=None)`。赋值另提供 `plan_batch` 和 `run_batch`。UI 只通过公共 API 和统一事件与后端交互。

历史性能与等价性结果见 [基准说明](benchmarks/README.md)，版本变更见 [CHANGELOG](CHANGELOG.md)。历史基准记录中的 GeoToolBox 是更名前的名称。

## 许可证

项目代码采用 [MIT License](LICENSE)。eCognition 和其他第三方组件仍适用各自许可；MIT 不授予商业 SDK 的使用权。依赖与内部 bridge 说明见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。
