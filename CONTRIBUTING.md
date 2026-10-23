# 参与开发

欢迎提交问题、补充硬件测试结果或改进代码。项目目前围绕 LX06 1.94.14 开发；其他版本请先建立独立的兼容性记录。

## 本地检查

需要 Python 3.11+、Bash 和 Node.js。打包测试还需要 `squashfs-tools`、`fakeroot`；完整镜像构建的依赖见[构建指南](docs/offline-build.md)。

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=build/pycache python3 -m compileall -q api tools tests
for f in scripts/*.sh diagnostic/*.sh; do bash -n "$f"; done
bash -n packages.sh
node --check api/static/app.js
```

默认测试不需要连接音箱。没有原厂文件时，6 项真实输入测试会跳过；提供自己的原厂解包目录后可运行全部测试：

```sh
LX06_STOCK_ROOTFS="$PWD/build/stock-rootfs" python3 -m unittest discover -s tests -v
```

GitHub Actions 运行无设备测试和语法检查，不下载厂商固件。

## 报告问题

请在 Issue 中说明：

- 音箱型号、主板/NAND 型号、原厂固件版本。
- 使用的仓库版本或提交、电脑系统、Python 和 USB 驱动版本。
- 操作步骤、预期结果和实际错误。
- 问题发生在 USB 识别、读取、构建、启动还是播放阶段。

日志只摘录相关错误，并删除 Wi-Fi 密码、令牌、设备标识和个人路径。不要上传完整设备备份或含配置的固件。

## 添加固件或硬件支持

1. 固定原厂输入的版本、来源、长度和 SHA256。
2. 为新版本单独建立 profile，核对补丁前后文件和硬件保护区。
3. 完成构建、依赖及解包检查，再记录设备上的测试结果。
4. 更新支持表，分别标注服务启动、实际播放和断电启动的结果。

现有 profile 和 USB 写入代码中的固定校验对应特定输入。新增支持应补齐这些依据，不应直接删除校验或替换为任意 hash。

## 代码来源

上游来源见 [UPSTREAM.md](UPSTREAM.md)。`packages/` 保留软件包构建配方；当前可复现路径使用固定上游 release 的组件差分，尚未重新编译全部软件包。新增或修改第三方代码时，请保留相应版权和许可说明。
