# 第三方组件与运行依赖

SegAssign 的 MIT 许可仅覆盖项目作者有权授权的代码和资源，不改变第三方组件的许可。

- **eCognition Developer 9**：外部商业软件，用户自行安装和授权。本仓库与发布包不包含其 SDK DLL 或许可证。
- **内部 bridge**：`assets/vendor/ecognition-9/bridge.exe` 是项目保留的 AMI 适配程序，不是 eCognition 引擎，也不提供独立业务入口。启动前校验 `assets/SHA256SUMS` 中的固定哈希。此仓库保留的是该适配程序的二进制，不能仅凭本仓库重新编译 bridge。
- **Python/GIS/Qt 依赖**：运行依赖列于 `pyproject.toml` 和 `environment.yml`，各自保留原有许可。Windows 包由本机 Conda 环境经 PyInstaller 收集运行库；其中第三方文件不改授 MIT。

规则集保存在 `assets/rulesets/`，校验哈希见 `assets/SHA256SUMS`。业务影像、客户成果、工作缓存及 eCognition SDK 不进入源码仓库。
