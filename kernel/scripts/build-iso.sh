#!/usr/bin/env bash
# Build nova.iso: FAT ESP with kernel + limine.conf, Limine UEFI+BIOS hybrid.
set -euo pipefail

KERNEL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(dirname "$KERNEL_DIR")"
PROFILE="${1:-release}"

# shellcheck disable=SC1091
source "$KERNEL_DIR/scripts/fetch-limine.sh"

KERNEL_ELF="$KERNEL_DIR/target/x86_64-unknown-none/$PROFILE/nucleus"
if [ ! -f "$KERNEL_ELF" ]; then
  echo "build-iso: kernel ELF missing at $KERNEL_ELF (run: make build)" >&2
  exit 1
fi

ISO_ROOT="$KERNEL_DIR/iso_root"
ESP_IMG="$REPO_ROOT/build/esp.fat"
ISO_OUT="$REPO_ROOT/build/nova.iso"
rm -rf "$ISO_ROOT" "$ESP_IMG"
mkdir -p "$ISO_ROOT/EFI/BOOT" "$REPO_ROOT/build"

install -m 0644 "$KERNEL_ELF" "$ISO_ROOT/NUCLEUS"
install -m 0644 "$KERNEL_DIR/limine.conf" "$ISO_ROOT/EFI/BOOT/limine.conf"
install -m 0644 "$LIMINE_BIN_DIR/BOOTX64.EFI" "$ISO_ROOT/EFI/BOOT/BOOTX64.EFI"
install -m 0644 "$LIMINE_BIN_DIR/BOOTIA32.EFI" "$ISO_ROOT/EFI/BOOT/BOOTIA32.EFI"
install -m 0644 "$LIMINE_BIN_DIR/limine-bios-cd.bin" "$REPO_ROOT/build/limine-bios-cd.bin"
install -m 0644 "$LIMINE_BIN_DIR/limine-bios.sys" "$REPO_ROOT/build/limine-bios.sys"

# 8 MiB FAT image, geometry matching an El Torito no-emulation boot image.
IMG_SECTORS=16384
dd if=/dev/zero of="$ESP_IMG" bs=512 count=$IMG_SECTORS status=none

if command -v mkfs.fat >/dev/null 2>&1; then
  mkfs.fat -F 12 --offset 63 "$ESP_IMG" >/dev/null
  MTOOLS_PREFIX="63/"
  export MTOOLS_PREFIX
  mcopy -s -i "$ESP_IMG" "$ISO_ROOT/EFI" ::/EFI
  mcopy -i "$ESP_IMG" "$ISO_ROOT/NUCLEUS" ::/NUCLEUS
elif command -v mformat >/dev/null 2>&1; then
  mformat -t 510 -h 4 -s 32 -C -T 16321 -o 63 -i "$ESP_IMG" ::
  MTOOLS_PREFIX="63/"
  export MTOOLS_PREFIX
  mcopy -s -i "$ESP_IMG" "$ISO_ROOT/EFI" ::/EFI
  mcopy -i "$ESP_IMG" "$ISO_ROOT/NUCLEUS" ::/NUCLEUS
else
  echo "build-iso: need mkfs.fat (dosfstools) or mtools" >&2
  exit 1
fi

# El Torito ISO: EFI System Partition image + BIOS boot image.
xorriso -as mkisofs \
  -b build/limine-bios-cd.bin -no-emul-boot -boot-load-size 4 -boot-info-table \
  --efi-boot build/esp.fat -efi-boot-part --efi-boot-image --protective-msdos-label \
  -o "$ISO_OUT" \
  -hide-rr-moved -graft-points \
  build/limine-bios-cd.bin=build/limine-bios-cd.bin \
  build/esp.fat=build/esp.fat \
  "$KERNEL_DIR/limine.conf=limine.conf" \
  2>&1 | grep -v -E '^(xorriso|libisoburn|I:|aac)' || true

# Point the BIOS boot image at the ISO and sync UEFI checksums.
if command -v "$LIMINE_BIN_DIR/limine-tool-windows-x86.exe" >/dev/null 2>&1; then
  "$LIMINE_BIN_DIR/limine-tool-windows-x86.exe" "$ISO_OUT"
elif command -v "$LIMINE_BIN_DIR/limine-tool-windows-x86" >/dev/null 2>&1; then
  "$LIMINE_BIN_DIR/limine-tool-windows-x86" "$ISO_OUT"
elif command -v limine >/dev/null 2>&1; then
  limine install "$ISO_OUT"
else
  echo "build-iso: WARNING: no limine-tool found; BIOS boot may need `limine install`" >&2
fi

echo "build-iso: wrote $ISO_OUT ($(wc -c < "$ISO_OUT") bytes)"
