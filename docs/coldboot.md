# 断电后黄灯不联网

## 问题现象

适配后通过 USB 重启可以联网，但拔除 USB、断电再接电时，音箱黄灯常亮，没有播报，也不加入 Wi-Fi。

这次排查中，断电前后完整 40 MiB `system1` 的哈希相同，`/data` 中的配置和网页也仍在。系统已经写入，问题发生在启动流程；重新刷相同镜像不能解释或解决这一现象。

## 启动路径

实测设备使用 U-Boot 2015.01（Feb 09 2026 06:51:13），其中：

```text
preboot=run storeargs;if test ${reboot_mode} = cold_boot; then run try_auto_burn; fi;
try_auto_burn=update 500 1000;
```

冷启动会先进入 `try_auto_burn`，USB normal reset 则不走这一分支。有效的处理是保留 USB recovery 的 `update`，在它返回后追加一次 reset：

```text
try_auto_burn=update 500 1000; reset;
```

修改后完成了一次拔除 USB 的断电启动，重新联网并恢复网页。观察到的 reboot reason 是 `watchdog_reboot`，也会跳过 cold-only 分支。这说明该绕过在这台设备上有效，但还不能确定原始故障在 U-Boot 内的具体位置。

## 修改前确认

本方法针对上述环境和现象。请先保存本机分区备份、完整环境变量，并确认可通过 USB recovery 或 U-Boot 控制台恢复环境。

使用 Linux 的 `fw_setenv` 前，应确认本机 `fw_env.config` 的分区位置和冗余设置正确，`fw_printenv` 能读出实际环境。不要套用其他音箱的 NAND 偏移。

以下是实测设备的其他关键启动变量，供比对启动逻辑，**不要整份导入**：

```text
storeboot=if test ${reboot_mode} = cold_boot; then run set_boot_flag; else run boot_failed; fi; saveenv; if imgread kernel ${boot_part} ${loadaddr}; then bootm ${loadaddr}; fi; reset;
set_boot_flag=if test ${boot_part} = boot0; then setenv boot_failcnt 1; else setenv boot_failcnt 2; fi;
boot_failed=if itest ${boot_failcnt} == 1; then setenv boot_failcnt 2; setenv boot_part boot1; else if itest ${boot_failcnt} == 2; then setenv boot_failcnt 1; setenv boot_part boot0; else run set_boot_flag;fi;fi;
factory_detect=echo no need detect now.
```

如果 `update` 本身卡住，追加 reset 不会执行；如果 reset 后仍被判断为 cold_boot，则可能反复重启。因此需要先准备恢复入口。

## 从已启动的 Linux 修改

下面的命令在音箱本机执行。先保存快照，并检查 `preboot`、`storeboot` 等变量是否符合上面的逻辑。

```sh
umask 077
mkdir -p /data/private-coldboot
work=/data/private-coldboot/$(date +%Y%m%d-%H%M%S)
mkdir "$work" || exit 1
fw_printenv > "$work/env.before" || exit 1
test "$(fw_printenv -n try_auto_burn)" = 'update 500 1000;' || exit 1
sync
```

确认备份和恢复入口后，在同一个 shell 中修改：

```sh
fw_setenv try_auto_burn 'update 500 1000; reset;' || exit 1
fw_printenv > "$work/env.after" || exit 1
test "$(fw_printenv -n try_auto_burn)" = 'update 500 1000; reset;' || exit 1
sync
```

重启前对比两个快照，应只有 `try_auto_burn` 一项变化。可将快照复制到自己的分析电脑，使用下面的 Python 脚本检查；脚本只输出检查结果，不打印环境内容。

```sh
python3 - env.before env.after <<'PY'
import sys
from pathlib import Path
def load(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        key, value = line.split('=', 1)
        assert key not in result, 'duplicate env key'
        result[key] = value
    return result
before, after = map(load, sys.argv[1:])
assert before.get('try_auto_burn') == 'update 500 1000;'
assert after.get('try_auto_burn') == 'update 500 1000; reset;'
assert {k:v for k,v in before.items() if k != 'try_auto_burn'} == {k:v for k,v in after.items() if k != 'try_auto_burn'}
print('only try_auto_burn changed')
PY
```

## 从 U-Boot 修改

如果已有可用的 U-Boot 控制台或经过验证的 USB env 入口，先保存完整 `printenv` 输出，确认 RAM 中没有其他待保存修改，再逐条执行：

```text
setenv try_auto_burn 'update 500 1000; reset;'
printenv try_auto_burn
saveenv
printenv
```

`saveenv` 会保存整个 RAM 环境。最终完整输出应与修改前仅有这一项不同，不应同时更改 `boot_part` 或 `boot_failcnt`。本项目没有通用 USB env 写入入口。

## 回滚

Linux 下恢复原值：

```sh
test "$(fw_printenv -n try_auto_burn)" = 'update 500 1000; reset;' || exit 1
fw_setenv try_auto_burn 'update 500 1000;' || exit 1
test "$(fw_printenv -n try_auto_burn)" = 'update 500 1000;' || exit 1
sync
```

U-Boot 下执行 `setenv try_auto_burn 'update 500 1000;'`，然后 `saveenv`，再检查完整环境。无法进入 Linux 时，使用修改前准备好的 recovery/控制台入口恢复这一项。

## 验证

拔除 USB，断开 DC 电源 10 秒后接回，不按键。确认音箱重新联网，并检查新的 boot_id、uptime、reboot reason、运行分区、Wi-Fi、启动记录和网页/API。

USB 重启成功不能替代这一步。当前记录只有一次独立供电冷启动成功，详细结果见[支持与验证情况](compatibility.md)。
