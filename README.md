# Xiaoai LX06 · 1.94.14

[![Checks](https://github.com/thaning0/xiaoai-lx06-19414/actions/workflows/check.yml/badge.svg)](https://github.com/thaning0/xiaoai-lx06-19414/actions/workflows/check.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

为小爱音箱 Pro（LX06）原厂固件 **1.94.14** 提供 [xiaoai-patch](https://github.com/duhow/xiaoai-patch) 适配补丁、构建工具和使用文档。

项目采用 xiaoai-patch 的开放音频方案，替换原生小爱服务，保留设备自己的内核、硬件驱动和校准数据。

## 功能

- **SSH 访问**：登录音箱，运行自定义程序、管理服务。
- **本地控制页面**：查看设备信息、调整三路音量；无需先配置 Home Assistant。
- **开放音频环境**：集成 MPD、Shairport Sync、UPMPDCLI、Snapclient 和 BlueALSA 等上游组件。
- **1.94.14 构建支持**：使用固定版本的原厂输入生成系统镜像，检查文件权限、依赖和硬件文件是否保留。
- **USB 与启动排障**：提供 Windows WinUSB 读取工具，以及断电后黄灯不联网的处理记录。

## 支持情况

| 项目 | 当前范围 |
| --- | --- |
| 设备 | 小米小爱音箱 Pro，型号 LX06 |
| 已实测硬件 | AS06 主板、board_id 3、FORESEE FSNS8A001G NAND，128 MB 变体 |
| 原厂固件 | **1.94.14** |
| 已验证 | Wi-Fi、SSH、本地网页/API、ALSA 播放测试音、一次拔除 USB 后的断电启动 |
| 待验证 | AirPlay / Bluetooth / UPnP 实际播放、麦克风、AUX、其他主板和固件版本 |

目前只有一台设备的实测记录。公开源码整理后重新生成的镜像已通过离线构建检查，尚未重新刷入设备；完整记录见[支持与验证情况](docs/compatibility.md)。此方案会替换原有小爱语音与配网功能。

## 开始使用

### 从源码构建

```sh
git clone https://github.com/thaning0/xiaoai-lx06-19414.git
cd xiaoai-lx06-19414
```

按照[构建指南](docs/offline-build.md)下载并校验输入、准备软件包、生成镜像。构建需要 Linux；USB 连接和读取使用原生 Windows。

镜像写入部分仍保留本次实测设备的专用校验，没有通用的一键刷机入口。请先阅读[USB 连接与备份](docs/windows-usb.md)及[刷写与恢复说明](docs/flashing-research.md)。

### 使用已经完成适配的音箱

在同一局域网打开 `http://<音箱 IP>/`，即可访问本地控制页面。SSH 登录方式（将 `SPEAKER_IP` 替换为音箱 IP）：

```sh
ssh root@SPEAKER_IP
```

默认构建沿用上游的 `root` 初始密码，首次登录后请运行 `passwd` 修改。网页没有独立的登录保护，适合在可信局域网使用。

遇到断电后黄灯常亮、没有联网，可参考[冷启动问题处理](docs/coldboot.md)；其他问题见[常见问题](docs/troubleshooting.md)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [构建指南](docs/offline-build.md) | 环境准备、固定输入、构建和产物检查 |
| [USB 连接与备份](docs/windows-usb.md) | Windows 驱动、读取命令、错误分类 |
| [刷写与恢复](docs/flashing-research.md) | 现有写入代码的适用范围和恢复原则 |
| [冷启动问题处理](docs/coldboot.md) | 问题现象、环境变量修改和回滚 |
| [支持与验证情况](docs/compatibility.md) | 硬件、固件版本和功能测试结果 |
| [常见问题](docs/troubleshooting.md) | USB 传输错误、文件权限、网页和 ADB |
| [参与开发](CONTRIBUTING.md) | 本地测试、提交问题、添加版本支持 |
| [更新记录](CHANGELOG.md) | 发布版本和主要改动 |

## 项目结构

```text
api/          本地控制页面与 API
profiles/     LX06 1.94.14 精确补丁及校验清单
tools/        构建、审计和 USB 工具
diagnostic/   启动诊断及 Wi-Fi 配置脚本
scripts/      镜像构建步骤
packages/     上游软件包构建配方
patches/      上游通用和机型补丁
tests/        无设备测试
docs/         构建、使用和排障文档
```

## 致谢与许可

本项目基于 **[duhow/xiaoai-patch](https://github.com/duhow/xiaoai-patch)**，感谢上游作者和贡献者。代码来源与修改范围见 [UPSTREAM.md](UPSTREAM.md)。

仓库采用 [GPL-3.0](LICENSE)，第三方文件保留原有许可，详见 [NOTICE](NOTICE)。仓库和 Release 提供源码，不包含厂商固件、设备备份或 Wi-Fi 配置；构建输入从原始发布地址获取。
