#!/usr/bin/env bash
# mount-ro.sh -- mount a possibly-failing drive/partition (or a ddrescue image)
# read-only, with filesystem-appropriate safety flags so nothing gets written
# back to the source, even incidentally (journal replay, hibernation cleanup).
#
# Usage:
#   mount-ro.sh <source> <mountpoint> [partition-number]
#
#   <source>           block device/partition (e.g. /dev/disk/by-id/usb-XXXX-part1)
#                       or a ddrescue image file (e.g. /data/rescue.img)
#   <mountpoint>        directory to mount onto (created if missing)
#   [partition-number]  only used when <source> is an image file that has a
#                       partition table: which partition to mount (default: 1).
#                       Ignored for block devices.
#
# Examples:
#   sudo ./mount-ro.sh /dev/disk/by-id/usb-WD_Elements-part1 /mnt/dying
#   sudo ./mount-ro.sh /data/rescue.img /mnt/rescue 2

set -euo pipefail

SOURCE="${1:?Usage: $0 <source> <mountpoint> [partition-number]}"
MOUNTPOINT="${2:?Usage: $0 <source> <mountpoint> [partition-number]}"
PART_NUM="${3:-1}"

if [[ $EUID -ne 0 ]]; then
    echo "Must run as root (mount requires it)." >&2
    exit 1
fi

DEVICE="$SOURCE"
LOOP_DEV=""

cleanup_on_error() {
    if [[ -n "$LOOP_DEV" ]]; then
        echo "==> mount failed, detaching $LOOP_DEV" >&2
        losetup -d "$LOOP_DEV" 2>/dev/null || true
    fi
}
trap cleanup_on_error ERR

if [[ -f "$SOURCE" ]]; then
    echo "==> '$SOURCE' is a regular file; attaching as a loop device with partition scan..."
    LOOP_DEV="$(losetup -fP --show "$SOURCE")"
    echo "==> attached as $LOOP_DEV"

    CANDIDATE="${LOOP_DEV}p${PART_NUM}"
    if [[ -b "$CANDIDATE" ]]; then
        DEVICE="$CANDIDATE"
    else
        echo "==> no partition table detected (or partition $PART_NUM not found); mounting $LOOP_DEV directly"
        DEVICE="$LOOP_DEV"
    fi
elif [[ -b "$SOURCE" ]]; then
    DEVICE="$SOURCE"
else
    echo "Error: '$SOURCE' is neither a block device nor a regular file." >&2
    exit 1
fi

if mountpoint -q "$MOUNTPOINT" 2>/dev/null; then
    echo "Error: something is already mounted at $MOUNTPOINT" >&2
    exit 1
fi
mkdir -p "$MOUNTPOINT"

FSTYPE="$(blkid -o value -s TYPE "$DEVICE" || true)"
echo "==> detected filesystem: ${FSTYPE:-unknown} on $DEVICE"

case "$FSTYPE" in
    ext2|ext3|ext4)
        # noload: skip journal replay entirely. Without it, the kernel can
        # still decide the journal needs replaying to present a consistent
        # view -- that's a write, even under a "ro" mount.
        mount -t "$FSTYPE" -o ro,noload "$DEVICE" "$MOUNTPOINT"
        ;;
    xfs)
        # norecovery is xfs's equivalent of ext4's noload.
        mount -t xfs -o ro,norecovery "$DEVICE" "$MOUNTPOINT"
        ;;
    ntfs)
        # remove_hiberfile lets a hibernated Windows volume mount at all, but
        # it does write to the volume to clear the hibernation flag. Drop it
        # (and accept the mount failing on hibernated volumes) if you need a
        # guaranteed zero-write mount.
        ntfs-3g -o ro,remove_hiberfile "$DEVICE" "$MOUNTPOINT"
        ;;
    "")
        echo "==> could not detect a filesystem type; attempting generic ro mount (auto-detect)"
        mount -o ro "$DEVICE" "$MOUNTPOINT"
        ;;
    *)
        mount -t "$FSTYPE" -o ro "$DEVICE" "$MOUNTPOINT"
        ;;
esac

trap - ERR
echo "==> mounted $DEVICE at $MOUNTPOINT (read-only)"
mount | grep -F "$MOUNTPOINT"

echo
if [[ -n "$LOOP_DEV" ]]; then
    echo "When done: umount $MOUNTPOINT && losetup -d $LOOP_DEV"
else
    echo "When done: umount $MOUNTPOINT"
fi
