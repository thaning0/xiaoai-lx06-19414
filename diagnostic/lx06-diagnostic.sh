#!/bin/sh
# Diagnostic records contain fixed labels and selected non-secret runtime fields.
LX06_DIAG_TMP=/tmp/lx06-diagnostic-v2.log
LX06_DIAG_DIR=/data/lx06-diagnostic

# adb-started acknowledges successful init-script invocation, not USB enumeration.
lx06_diag_event() {
    case "$1" in
        init-entry|adb-started|adb-failed|data-mounted|data-mount-failed|reformat-skipped|wifi-seeded|wifi-already-seeded|wifi-seed-failed|late-init-done) ;;
        *) return 1 ;;
    esac
    [ -f "$LX06_DIAG_TMP" ] || (umask 077; : > "$LX06_DIAG_TMP")
    [ "$(wc -c < "$LX06_DIAG_TMP")" -lt 245760 ] || return 0
    printf '%s\n' "$1" >> "$LX06_DIAG_TMP"
}

lx06_diag_safe_dir() {
    [ -d /data ] && [ ! -L /data ] && [ ! -L "$1" ] || return 1
    if [ ! -e "$1" ]; then (umask 077; mkdir "$1") || return 1; fi
    [ -d "$1" ] && [ ! -L "$1" ] && chmod 700 "$1"
}

lx06_diag_snapshot() {
    [ "$(wc -c < "$LX06_DIAG_TMP")" -lt 245760 ] || return 0
    { printf 'snapshot-begin\n';
      uname -r 2>/dev/null | head -c 128; printf '\n';
      awk '$2 == "/data" || $2 == "/proc" || $2 == "/sys" {print $2, $3}' /proc/mounts | head -n 8;
      dmesg 2>/dev/null | tail -n 80 | awk '
          /[Uu][Bb][Ii][Ff][Ss]/ {print "dmesg:ubifs"; next}
          /[Uu][Bb][Ii]/ {print "dmesg:ubi"; next}
          /[Mm][Tt][Dd]/ {print "dmesg:mtd"; next}
          /8821/ {print "dmesg:8821"; next}
          /[Ww][Ll][Aa][Nn]/ {print "dmesg:wlan"; next}
          /[Uu][Ss][Bb]/ {print "dmesg:usb"; next}
          /[Ee][Rr][Rr][Oo][Rr]|[Ff][Aa][Ii][Ll]/ {print "dmesg:error"}
      ' | head -n 40;
      for netif in wlan0 usb0; do
          [ -e "/sys/class/net/$netif" ] && printf 'interface=%s\n' "$netif"
      done
      wpa_cli -i wlan0 status 2>/dev/null | grep -E '^(wpa_state|ip_address)=' | head -n 2;
      printf 'snapshot-end\n';
    } >> "$LX06_DIAG_TMP"
    # A hard ceiling even if command output unexpectedly grows.
    if [ "$(wc -c < "$LX06_DIAG_TMP")" -gt 262144 ]; then
        head -c 262144 "$LX06_DIAG_TMP" > "${LX06_DIAG_TMP}.cut" && mv "${LX06_DIAG_TMP}.cut" "$LX06_DIAG_TMP"
    fi
}

lx06_diag_persist() {
    grep -q ' /data ubifs ' /proc/mounts || return 1
    lx06_diag_safe_dir "$LX06_DIAG_DIR" || return 1
    [ ! -L "$LX06_DIAG_DIR/startup.log" ] || return 1
    (umask 077; cp "$LX06_DIAG_TMP" "$LX06_DIAG_DIR/startup.log")
}

lx06_diag_seed_wifi() {
    grep -q ' /data ubifs ' /proc/mounts || return 1
    lx06_diag_safe_dir "$LX06_DIAG_DIR" || return 1
    [ ! -L "$LX06_DIAG_DIR/wifi-seeded-v1" ] || return 1
    [ ! -e "$LX06_DIAG_DIR/wifi-seeded-v1" ] || { lx06_diag_event wifi-already-seeded; return 0; }
    for path in /data/wifi /data/status; do
        [ ! -L "$path" ] || return 1
        if [ ! -e "$path" ]; then (umask 022; mkdir "$path") || return 1; fi
        [ -d "$path" ] || return 1
    done
    [ ! -L /data/status/config_done ] || return 1
    dest=/data/wifi/wpa_supplicant.conf
    backup="$LX06_DIAG_DIR/wpa_supplicant.conf.before-v1"
    [ ! -L "$dest" ] && [ ! -e "$backup" ] && [ ! -L "$backup" ] || return 1
    if [ -e "$dest" ]; then
        [ -f "$dest" ] && ln "$dest" "$backup" || return 1
        chmod 600 "$backup" || return 1
    fi
    tmp="/data/wifi/.lx06-diagnostic-wpa.$$"
    (umask 077; set -C; cat /etc/lx06-diagnostic/wpa_supplicant.conf > "$tmp") || return 1
    chmod 600 "$tmp" && mv -f "$tmp" "$dest" || { rm -f "$tmp"; return 1; }
    [ ! -L /data/status/config_done ] || return 1
    [ -f /data/status/config_done ] || (umask 077; set -C; : > /data/status/config_done) || return 1
    (umask 077; set -C; printf 'v1\n' > "$LX06_DIAG_DIR/wifi-seeded-v1") || return 1
    lx06_diag_event wifi-seeded
}
