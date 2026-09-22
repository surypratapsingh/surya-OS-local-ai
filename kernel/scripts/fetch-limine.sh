#!/usr/bin/env bash
# Locate a Limine v12.x binary distribution, preferring the vendored copy.
# Exposes: LIMINE_BIN_DIR (with BOOTX64.EFI, BOOTIA32.EFI, limine-bios-cd.bin,
# limine-bios.sys, limine-tool*).
set -euo pipefail

KERNEL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(dirname "$KERNEL_DIR")"
VENDORED="$REPO_ROOT/.freebuff/ref/limine/limine-binary"

if [ -f "$VENDORED/BOOTX64.EFI" ]; then
  echo "fetch-limine: using vendored Limine ($VENDORED)"
  LIMINE_BIN_DIR="$VENDORED"
elif [ -n "${LIMINE_DIR:-}" ]; then
  echo "fetch-limine: using LIMINE_DIR=$LIMINE_DIR"
  LIMINE_BIN_DIR="$LIMINE_DIR"
else
  VER="v12.9.0"
  DEST="$REPO_ROOT/build/limine"
  mkdir -p "$DEST"
  if [ ! -f "$DEST/BOOTX64.EFI" ]; then
    echo "fetch-limine: downloading Limine $VER binary tarball"
    curl -fL --retry 3 -o "$DEST/limine.tar.gz" \
      "https://github.com/Limine-Bootloader/Limine/releases/download/$VER/limine-binary.tar.gz"
    tar -xzf "$DEST/limine.tar.gz" -C "$DEST" --strip-components=1
  fi
  LIMINE_BIN_DIR="$DEST"
fi

export LIMINE_BIN_DIR
echo "fetch-limine: LIMINE_BIN_DIR=$LIMINE_BIN_DIR"
