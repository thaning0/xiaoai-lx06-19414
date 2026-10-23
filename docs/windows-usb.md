# Windows USB 连接与备份

本文介绍如何识别恢复模式中的 LX06，以及使用 WinUSB 读取命名分区。先核对[支持范围](compatibility.md)，并准备足够空间保存本机备份。

## 环境准备

实测成功组合是**原生 Windows、实际 PnP Service=WinUSB、PyUSB 的 libusb1 backend 与 libusb-1.0.dll**。VID/PID 为 `1b8e:c003`，identify 回应 `00070010`。DLL 位数须与 Python 相同；DLL 文件名与驱动安装包名不能证明实际绑定成功。`lx06_usb_read.py` 会重新查 PnP Service；bank probe 更要求唯一设备且 Status=OK。不要绕过 guard 或换回 libusb0 作为自动 fallback。

安装 PyUSB，准备与 Python 位数一致的 libusb-1.0.dll，并确认只为目标音箱绑定 WinUSB 驱动。以下示例把 DLL 放在 `host-tools/` 下。

## 识别与读取

在仓库目录打开 PowerShell：

```powershell
py -m pip install pyusb
New-Item -ItemType Directory private-backups
py tools/lx06_usb_read.py --dll .\host-tools\libusb-1.0.dll identify
py tools/lx06_usb_read.py --dll .\host-tools\libusb-1.0.dll read boot0 --length 0x600000 --output .\private-backups\boot0-first
py tools/lx06_usb_read.py --dll .\host-tools\libusb-1.0.dll read boot1 --length 0x600000 --output .\private-backups\boot1-first
py tools/lx06_usb_read.py --dll .\host-tools\libusb-1.0.dll read system0 --length 0x2800000 --output .\private-backups\system0-first
py tools/lx06_usb_read.py --dll .\host-tools\libusb-1.0.dll read system1 --length 0x2800000 --output .\private-backups\system1-first
py tools/lx06_usb_read.py --dll .\host-tools\libusb-1.0.dll read data --length 0x1400000 --output .\private-backups\data-first
```

## 复核备份

每条成功后检查退出码和 manifest；失败立即停下，保留 partial/failure，不批量重试或把不完整输出改名当备份。先做一次完整备份，再在新目录独立回读比较。例如，只有第一份 boot0 长度正确且 manifest 成功时：

```powershell
$ref = '.\private-backups\boot0-first\boot0.img'
$hash = (Get-FileHash -Algorithm SHA256 $ref).Hash.ToLower()
py tools/lx06_usb_read.py --dll .\host-tools\libusb-1.0.dll read boot0 --length 0x600000 --output .\private-backups\boot0-second --reference $ref --reference-sha256 $hash
```

对其他分区各自计算自己的 SHA256，不套用别机 hash。文件必须完整 64 KiB 对齐；reader 固定允许分区名及读取上限，不提供任意读写命令。实际读取可揭示私人配置、标识和凭据，manifest 的 PnP 信息也可能含设备标识；备份、原始日志和 JSON 全部留在私人存储。

## 分区范围

| 分区 | 该设备逻辑读取长度 |
| --- | ---: |
| boot0、boot1 | 各 6291456 字节 / 6 MiB |
| system0、system1 | 各 41943040 字节 / 40 MiB |
| data | 20971520 字节 / 20 MiB |

这些是命名分区的逻辑字节范围，不是 raw NAND/OOB 或物理坏块全覆盖备份，也不能证明另一个变体布局相同。data 逻辑读出不等于可直接安全回灌的文件级备份；本项目没有通用恢复器。不要把 boot 分区文件当作可替换 kernel 的公开通用输入。

## 错误分类

设备返回 `failed:` 表示命令被拒绝；USB transport error 表示传输失败；无效命令探针也可能被正常拒绝，不能一概归因线材。libusb0 路线多次失败，但没有单一根因证明。bank probe 通过成对的真/假校准命令和重复观测推断 live boot_part，仅作只读研究；不是持久化读取或启动成功证明。
