# SegAssign 2.0.2

遥感科研项目配套自动化工具的首次公开发布。

- 统一 SegAssign 项目名、`segassign` Python 包及 `SegAssign.exe` 桌面入口。
- 提供影像分割、矢量赋值、分割后赋值三个任务入口。
- 支持 Shapefile 文件夹批处理，以及附带原始计数文件的非重叠区域二次赋值。
- 保留 TIFF 读取重试、具体影像/窗口错误诊断、已完成 tile 断点恢复。
- 项目代码采用 MIT 协议；商业 eCognition SDK 与许可证需用户自行提供。

Windows 用户请完整解压 `SegAssign-v2.0.2-Windows-x64.zip` 并运行 `SegAssign.exe`，保留 `_internal` 目录。00/01 分割要求本机安装并授权 eCognition Developer 9；02 赋值无需该软件。

跨区域赋值时保留 `.gtz.npz` 及 Shapefile 伴随文件，区域分类编码与像元面积需一致、有效区域不重叠。重复输入不会自动去重。断点续跑需要保持输入及相关路径/参数一致；程序不会在退出或断电后自动重启。

本次验证：46 项小样本回归测试与静态检查通过；另检查打包后的 UI 启动与 GIS 赋值。完整客户数据和商业引擎任务未在本次发布中重新运行。历史基准结果及其等价性限制见仓库 `benchmarks/README.md`。
