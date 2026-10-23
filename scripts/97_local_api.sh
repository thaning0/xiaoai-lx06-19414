#!/bin/sh
# Use the reviewed local API/UI rather than the reused release copy.
set -eu
if [ "$MODEL" = lx06 ] && [ "${LX06_OFFLINE:-0}" = 1 ]; then
  test -d "$ROOTFS/usr/share/api"
  rm -rf "$ROOTFS/usr/share/api"
  mkdir -p "$ROOTFS/usr/share/api"
  cp -R api/. "$ROOTFS/usr/share/api/"
  find "$ROOTFS/usr/share/api" -type d -exec chmod 755 {} +
  find "$ROOTFS/usr/share/api" -type f -exec chmod 644 {} +
fi
