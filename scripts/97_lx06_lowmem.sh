#!/bin/sh
# The pinned release overlay contains an unconditional Squeezelite init.
# Apply the current profile policy after all package/include copies.
set -eu
[ "$MODEL" = lx06 ] || exit 0
selection=0
python3 tools/lx06_profile.py select "$ROOTFS" || selection=$?
case "$selection" in
  0) exec python3 tools/lx06_profile.py lowmem "$ROOTFS" ;;
  3) exit 0 ;;
  *) exit "$selection" ;;
esac
