# 构建指南

本文使用固定的原厂固件和上游软件包生成 `root.squashfs`。整个构建在 Linux 上完成，不需要连接音箱。

构建分为两层：基础镜像包含开放音频服务和本地网页；诊断层补充启动记录、Wi-Fi 配置，并在 data 挂载失败时停止处理，避免自动格式化。

## 1. 准备环境

需要系统 `/usr/bin/python3` 为 3.11+，以及支持非特权 user namespace 的 Linux。构建隔离区使用系统 Python，只升级虚拟环境中的版本不够。Debian / Ubuntu 可安装以下依赖：

```sh
sudo apt-get install python3 bash bubblewrap fakeroot squashfs-tools patch rsync binutils curl
mkdir -p build/inputs build/outputs
```

构建入口为 `tools/lx06_offline_build.py`。仓库中保留的上游 `Makefile` 不是本文使用的构建入口。

## 2. 下载并校验输入

| 输入 | 用途 |
| --- | --- |
| LX06 1.94.14 原厂固件 | 当前系统基础，保留对应硬件文件 |
| LX06 1.74.10 原厂固件 | 对比上游 release，提取软件包差分 |
| xiaoai-patch 2026.3.26 LX06 release | 提供开放音频软件包 |

```sh
curl -fL 'https://cdn.cnbj1.fds.api.mi-img.com/xiaoqiang/rom/lx06/mico_all_ce7185fc5_1.94.14.bin' -o build/inputs/mico_all_ce7185fc5_1.94.14.bin
curl -fL 'https://cdn.cnbj1.fds.api.mi-img.com/xiaoqiang/rom/lx06/mico_firmware_9c712_1.74.10.bin' -o build/inputs/mico_firmware_9c712_1.74.10.bin
curl -fL 'https://github.com/duhow/xiaoai-patch/releases/download/2026.3.26/mico_firmware_080b2a0_260329_lx06.tar' -o build/inputs/mico_firmware_080b2a0_260329_lx06.tar
sha256sum -c docs/inputs.sha256
python3 - <<'PY'
import json
from pathlib import Path
for item in json.loads(Path('docs/inputs.json').read_text())['downloads']:
    assert (Path('build/inputs') / item['filename']).stat().st_size == item['size']
print('input sizes passed')
PY
```

完整来源、长度和 SHA256 见 [inputs.json](inputs.json)。校验通过后，后续步骤可离线运行。

上游 release 仅用于提取 rootfs 中的软件组件。**不要执行其中的 `update.sh`，也不要使用其中的 `boot.img` 替换本机内核。**

## 3. 提取软件包并构建基础镜像

```sh
python3 tools/mico_firmware.py -e build/inputs/mico_all_ce7185fc5_1.94.14.bin -d build/stock-19414
python3 tools/mico_firmware.py -e build/inputs/mico_firmware_9c712_1.74.10.bin -d build/stock-17410
python3 tools/lx06_release_overlay.py --original-squashfs build/stock-17410/root.squashfs --release-tar build/inputs/mico_firmware_080b2a0_260329_lx06.tar --output build/armv7-overlay
python3 tools/lx06_offline_build.py --squashfs build/stock-19414/root.squashfs --packages build/armv7-overlay --output build/outputs/base
```

输出目录必须是新目录。重跑时换一个目录名，保留原结果供比较。

主要产物：

| 路径 | 内容 |
| --- | --- |
| `build/outputs/base/root.squashfs` | 基础系统镜像 |
| `build/outputs/base/build-manifest.json` | 输入、源码、镜像校验值和工具版本 |
| `build/outputs/base/work/original-tree.json` | 原厂文件清单 |
| `build/outputs/base/work/patched-tree.json` | 补丁后的文件清单 |
| `build/outputs/base/work/candidate.tree-audit.json` | 重新解包后的完整检查结果 |

基础镜像直接安装当前仓库的 `api/`，包括独立于 Home Assistant 的本地控制页面。

## 4. 加入 Wi-Fi 配置与启动诊断

创建 Wi-Fi 配置文件，将示例名称和密码替换成自己的配置。文件内容会进入最终镜像。

```sh
umask 077
cat > build/example-wifi.conf <<'CONF'
ctrl_interface=/var/run/wpa_supplicant
network={
    ssid="example-ssid"
    psk="example-password"
}
CONF
chmod 600 build/example-wifi.conf
python3 tools/lx06_diagnostic_build.py --base-image build/outputs/base/root.squashfs --base-report build/outputs/base/build-manifest.json --private-wifi-file build/example-wifi.conf --output build/outputs/diagnostic
```

结果位于 `build/outputs/diagnostic/root.squashfs`。镜像包含 Wi-Fi 配置，`build/` 默认被 Git 忽略；不要把这个目录或镜像上传到公开仓库。

诊断构建需要保留基础构建的**完整目录**，不能只复制镜像和 manifest。程序会校验原始输入、当前源码、冻结的软件包和文件清单，重新构建并比较结果，再应用诊断补丁。旧版本使用的 `BASE_PIN` 不再是有效输入。

Wi-Fi 种子在首次需要时写入 `/data`；已有配置不会因此自动重置。诊断脚本还会记录启动阶段，但其中的 `adb-started` 标记不表示 ADB 已正常枚举。

## 5. 检查结果

构建成功后，manifest 的状态为 `offline-built-hardware-unverified`。它表示离线检查通过，设备启动和播放仍需另外测试。

构建工具会检查：

- SquashFS 4.0、xz、128 KiB block，镜像严格小于 40 MiB。
- 原厂驱动、内核模块、固件和校准文件未被替换。
- ARM ELF 依赖和启动脚本语法。
- 打包前后的文件内容、mode、uid/gid、符号链接和 `/dev/console`。

诊断层使用 11 项明确的权限规则；允许已正确的权限，拒绝其他异常值。它的输出按 16 KiB 对齐。固定构建时间为 `1770618789`。

需要运行原厂输入测试时：

```sh
unsquashfs -no-progress -excludes -d build/stock-rootfs build/stock-19414/root.squashfs dev/console
LX06_STOCK_ROOTFS="$PWD/build/stock-rootfs" python3 -m unittest discover -s tests -v
```

镜像写入与备份要求见[刷写与恢复说明](flashing-research.md)。

## 构建原理与限制

软件包来自固定的上游 release 差分，源码提交为 `fb070495955668f825700bbd988d5443e9bcadb3`；本流程没有重新编译全部软件包。importer 排除硬件保护区、版本信息和非 ARM ELF，提取清单记录在 overlay 旁的 `.source.json` 中。

`packages.sh` 和 `packages/` 提供上游源码编译配方，可供进一步开发；该路线尚未纳入本文的完整验证。GCC 的可选缓存归档未随仓库分发，依赖由其 `download_prerequisites` 获取。

固件解析器验证容器结构、CRC、签名区长度和文件名短 MD5，未验证 OTA 加密签名。请先核对完整 SHA256，不要使用 `python -O`；提取器面向上述固定输入，不用于解析未知固件。

构建只运行仓库内的主机脚本，固件作为数据处理，不执行目标 ARM 程序。若 bubblewrap 无法创建隔离环境，请检查 user namespace 支持，或换到支持它的 Linux 环境。
