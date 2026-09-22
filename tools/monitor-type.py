#!/usr/bin/env python3
"""Type keys into a QEMU guest via the monitor TCP socket.

Usage: monitor-type.py <host> <port> <key1 key2 ...>
Key names follow QEMU's `sendkey` naming (q, 1, ret, spc, shift, ...).
"""
import socket
import sys
import time


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    host, port = sys.argv[1], int(sys.argv[2])
    keys = sys.argv[3].split()

    s = socket.create_connection((host, port), timeout=15)
    s.settimeout(3)

    def drain() -> bytes:
        out = b""
        try:
            while True:
                d = s.recv(4096)
                if not d:
                    break
                out += d
                if out.endswith(b"(qemu) "):
                    break
        except socket.timeout:
            pass
        return out

    drain()  # greeting + banner
    for k in keys:
        s.sendall(f"sendkey {k}\n".encode())
        time.sleep(0.5)
        drain()
    time.sleep(1.0)
    try:
        s.sendall(b"quit\n")
    except OSError:
        pass
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
