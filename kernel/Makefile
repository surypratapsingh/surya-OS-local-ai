# NOVA/Nucleus kernel tasks (K1)
# Windows note: run inside Git Bash (make from MSYS2) or WSL; scripts detect
# the platform and use limine-tool-windows-x86.exe where needed.

PROFILE ?= release
QEMU_ARGS += -M q35 -m 512M

.PHONY: all build iso disk run run-uefi run-disk check clean

all: build

build:
	cargo build --$(PROFILE)

fetch-limine:
	bash scripts/fetch-limine.sh

iso: build fetch-limine
	bash scripts/build-iso.sh $(PROFILE)

# Bootable pendrive/disk image: BIOS + UEFI (pure-Python builder, no mtools).
disk: build fetch-limine
	bash ../../scripts/build-disk.sh $(PROFILE)

run: iso
	bash scripts/run-qemu.sh

run-uefi: iso
	QEMU_UEFI=1 bash scripts/run-qemu.sh

run-disk: disk
	QEMU_DISK=1 bash scripts/run-qemu.sh

# CI-style: build, disk image, structural verify, QEMU boot (BIOS+UEFI when
# QEMU is available; clean SKIP otherwise).
check: disk
	bash ../../scripts/check.sh

clean:
	cargo clean
	rm -rf iso_root nova.iso ../../build
