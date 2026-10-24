# 常见问题

## USB 一直有传输错误

先区分三个层次：

| 现象 | 优先检查 |
| --- | --- |
| 找不到 VID `1b8e` / PID `c003` | 是否进入恢复模式、USB 枚举和连接情况 |
| 能枚举，但传输报错 | 实际绑定的驱动、libusb 后端、DLL 与 Python 位数 |
| 收到 `failed:` | 设备拒绝了命令；检查命令和当前状态 |

这次成功使用的是原生 Windows、WinUSB 和 libusb-1.0。早期 libusb0 路线多次失败，但没有证据把所有失败归因于线材、接口、电脑或“固件太新”。数据线能给其他设备传文件，也不能替代对当前 USB 协议的检查。

具体命令见[USB 连接与备份](windows-usb.md)。避免在一次传输失败后不断自动重试，先保留失败位置和响应。

## 断电以后只有黄灯，系统是不是没保存？

先比较分区内容和配置，再判断启动问题。本次完整 `system1` 回读与断电前一致，配置也保留；USB reset 后能够启动。最终使用了针对 cold_boot 分支的环境变量绕过，见[冷启动问题处理](coldboot.md)。

## 镜像大小、哈希都正确，为什么还是不能启动？

镜像格式正确不代表里面的启动文件权限正确。早期镜像中有五个关键文件丢失执行位：

```text
bin/wpa_action.sh
etc/init.d/bluetooth
etc/init.d/boot
etc/init.d/dropbear
usr/sbin/boot_function.sh
```

排查发现物理文件权限与 fakeroot 中观察到的权限不一致。当前打包使用清洁环境，并重新解包检查全部文件的内容和元数据；诊断层另有 11 项精确权限规则。不要对整个 rootfs 执行批量 chmod 来修复。

## 网页只有 “Home Assistant instances found”

上游页面以 Home Assistant 发现结果为主要入口，空结果时缺少本地控制内容。当前页面独立显示设备信息和三路音量，Home Assistant 配置为可选项。

新构建会复制仓库里的 `api/`。如果仍看到旧页面，检查实际部署的静态文件、浏览器缓存，以及页面/API 请求是否成功。

## 网页运行几十分钟后消失，但 SSH 仍正常

先查看 `dmesg` 中的 OOM 和被杀进程，再检查 `ubus call service list` 中 API 的 `web` 实例。已观察到原厂 `syslog-ng` 反复 abort、内部 supervisor 持续重启；原厂 `coredump.sh` 直接追加 maps 到 `/tmp/log/messages`，并在 tmpfs 压缩 core、尝试写入 crash 分区。这会放大内存压力。具体 abort 原因尚未确定，不能仅凭库被替换就认定 ABI 不兼容。

当前 LX06 1.94.14 精确 profile 保留 `S50syslog-ng` 入口，改由 procd 管理已有 BusyBox 1.36.1 的 `syslogd -n -C64 -D -f /dev/null` 和 `klogd -n`。用户态、内核和崩溃摘要共用 **64 KiB RAM 环形日志**；满后覆盖旧记录，不写 `/data`。修复随镜像持久保存，日志本身不跨重启保存。崩溃 hook 只记录摘要，不再追加 maps、生成 core 或调用 `mtd_crash_log`；需要完整 core 时应另做一次受控取证。

读取新日志：

```sh
/bin/busybox logread
dmesg
ubus call service list
```

日志修复本身不调整 API、音频或 HA 服务，也不修改它们的 respawn 参数；Squeezelite 的独立启动策略见下一节。日志服务使用有限重试参数 `3600 5 5`；不要把所有服务的 retry 统一改为 `0`，旧 procd 的具体语义需单独验证。

已刷设备优先按[构建指南](offline-build.md)重建基础镜像和诊断层，并按[刷写说明](flashing-research.md)执行原有备份及校验流程。镜像内修复不依赖 `/data` 或 `@reboot`。旧构建报告与当前源码不匹配时必须重建，不能只改报告 hash。

暂用 `/data` bind mount 时，应从新镜像取出上述两个文件，保留原权限并备份现有挂载脚本。只在确认 `/data` 已挂载后安装它们；重复执行应识别已有挂载，不叠加 mount。切换前保存证据，先停止经过进程树确认的旧 logger supervisor 及其子进程，再启动新的 init 服务；原厂 `stop` 可能因 PID file 失效，替换后的 procd `stop` 也不能接管旧 supervisor。不要用仅杀子进程或重启 API 的方式处理。

`@reboot` 晚于 `S50` 时，原厂 logger 已经启动；恢复 bind mount 的同时还必须完成上述 supervisor 交接。此方案仍有开机窗口，不能等同于镜像内修复。回滚应先停止新日志实例，卸载这两个 bind mount、恢复原挂载脚本，再按需恢复旧服务；移除持久挂载配置后重启也可恢复只读镜像中的原文件。旧 logger 的崩溃风险会同时恢复。

部署后至少观察 60 分钟，并做一次冷启动：确认两个日志实例、API 和音频服务正常，没有新的 OOM、重复 logger、core 或 messages 增长。已有大日志/core 需在导出证据并确认写入者退出后由部署者一次性清理；本修复不会定时删除日志。

## 日志已受控，为什么仍有音频进程 OOM？

有界日志只消除日志/core 放大路径。128 MB RAM 的 AS06/FORESEE 配置上，日志恢复后仍观察到 Squeezelite 反复被 OOM 杀死：它锁住约 30 MB 内存、RSS 约 28 MB，当时未发现 LMS TCP 连接。停止这个可选客户端后，API 正常，可用内存回升至约 50 MB。这不证明 Squeezelite 在所有配置下都会 OOM，也不能把所有 OOM 归因于日志。

当前 **LX06 1.94.14 profile** 保留 Squeezelite 程序、配置和 S99 入口，默认 `boot` 不启动它。用户可用 `/etc/init.d/squeezelite start` 手动启动；创建普通文件 `/data/squeezelite-autostart` 后，下次开机才会自动启动。移除 marker 只改变后续开机策略，需要停止当前实例时仍应显式执行 `stop`。其他机型的默认值及其他音频服务未调整，原有手动运行的 respawn 参数也未改动。

这一策略在固定 release 软件包复制之后应用，构建会校验原 init 和结果 hash；仅修改 `packages/` 配方不能更新当前离线镜像。新镜像在 S99 前就包含该策略；已刷设备的 `/data` overlay 则必须处理 `@reboot` 与 S99 的竞态，不能把“挂载成功”当作“旧启动动作已停止”。

主动启用 Squeezelite、并行启用多个语音/音频服务时，应重新评估 MemAvailable、VmLck、RSS 和 OOM 记录，并测试实际播放。默认启动调整不代表所有播放路径或长期稳定性已经验收，当前状态见[兼容性记录](compatibility.md)。

## 日志有 `adb-started`，但 adb 找不到设备

这个标记只表示启动脚本返回成功。还要检查 adbd 进程、USB gadget 和 UDC 绑定。本次设备的运行态 ADB 没有成功，不能仅根据标记判断已连接。

## 基础镜像报告校验失败

诊断构建要求镜像、完整构建目录和当前源码匹配。更新源码、只拷贝一个镜像、丢失 `work/`，或手工修改报告，都会导致拒绝。

按[构建指南](offline-build.md)在新目录重新生成基础镜像，再传入对应的 `--base-report`。不要改 hash 来迁就输入。

## bubblewrap 无法启动

构建环境需要支持非特权 user namespace。检查 Linux 系统限制或使用支持它的构建环境；不要将固件脚本改为直接在宿主机执行。

## 服务在运行，是否说明播放功能正常？

进程存在只能说明服务启动。AirPlay、蓝牙、UPnP 等还需要客户端连接和完整播放测试。当前确认的是 ALSA 测试音，其他功能状态见[支持与验证情况](compatibility.md)。

提交新问题时，请参考[参与开发](../CONTRIBUTING.md)，附上相关错误和环境信息，并去掉凭据及设备标识。
