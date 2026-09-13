# 版本记录

## 2.0.2 — SegAssign 首次公开发布

- 定位为遥感科研配套自动化工具，统一 UI、Python 包和 EXE 名称。
- Python 包由 `geotoolbox` 更名为 `segassign`，不提供旧包名兼容别名。
- 采用 MIT 协议，补充开发环境、运行限制、第三方组件说明和历史基准解释。
- 保留 `.geotoolbox-work` 缓存目录约定；处理算法与 2.0.1 一致。

## 2.0.1 — 读取恢复与错误诊断

- TIFF 读取失败后更换句柄，最多共尝试三次；持续失败时报告源文件、tile、窗口及底层错误。
- 准备阶段异常后，保存已经完成的 eCognition 任务断点。
- 增加截断 TIFF、瞬时错误、取消重试及恢复后不重算已完成 tile 的回归测试。

## 2.0.0 — 后端重构与桌面 UI

- 独立 segment、zonal、composite API；UI 通过统一事件消费后台状态。
- 分块读取、STRtree 候选查询、bincount 计数、指纹断点与输出事务。
- 原生 Shapefile 文件夹批处理和跨非重叠区域计数累加。
