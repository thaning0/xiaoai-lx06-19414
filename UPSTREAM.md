# 上游与代码来源

本项目派生自 [duhow/xiaoai-patch](https://github.com/duhow/xiaoai-patch)，保留其 GPL-3.0 许可证和各文件原有声明。

| 来源 | 版本 |
| --- | --- |
| xiaoai-patch 代码基础 | `c26bef82ed703b50e1c04b618b0f3106061dd301` |
| LX06 适配开发快照 | `813b0f0`，本项目导出前的本地提交 |
| 构建所用上游 release | [2026.3.26](https://github.com/duhow/xiaoai-patch/releases/tag/2026.3.26) |
| release 对应源码 | `fb070495955668f825700bbd988d5443e9bcadb3` |

本项目的主要改动：

- LX06 1.94.14 精确 profile 和硬件文件保护检查。
- Windows WinUSB 读取、设备专用写入协议及测试。
- 隔离构建、ELF 依赖审计和打包后文件权限校验。
- 启动诊断、Wi-Fi 配置种子及冷启动问题处理。
- 独立本地控制页面和当前 API/UI 的构建集成。
- 中文使用文档、回归测试和持续集成。

保留的其他机型补丁属于继承的上游代码，不表示这些机型已在本项目中测试。

构建使用的厂商输入和软件包来源见 [docs/inputs.json](docs/inputs.json)。上游 release 标注为未测试，本项目的实测范围见[支持与验证情况](docs/compatibility.md)。第三方许可见 [NOTICE](NOTICE)。
