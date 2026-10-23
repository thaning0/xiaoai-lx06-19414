#!/bin/sh

# Exact known stock profile: selected failures must never reach the legacy loop.
if [ "$MODEL" = "lx06" ]; then
  python3 tools/lx06_profile.py select "$ROOTFS"
  selection=$?
  case "$selection" in
    0) exec python3 tools/lx06_profile.py apply "$ROOTFS" ;;
    3) ;; # Other valid metadata keeps the existing multi-version patch behavior.
    *) exit "$selection" ;;
  esac
fi

if ! command -v patch >/dev/null ; then
  echo "[!] patch command not found, failing hard!"
  exit 1
fi

echo "[*] Applying patches"
for PATCHFILE in patches/*.patch patches/${MODEL}/*.patch; do
  echo ">> ${PATCHFILE}"
  patch -p1 --no-backup-if-mismatch -r /dev/null -d ${ROOTFS} < ${PATCHFILE}
done

true
