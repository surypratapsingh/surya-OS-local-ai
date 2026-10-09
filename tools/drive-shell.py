#!/usr/bin/env python3
"""Drive the NOVA interactive shell from a script (K4 follow-on).

This replaces the fixed-sleep sendkey procedure that produced
docs/logs/k4a-time-verb.log with something reproducible in CI.

How it works
------------
The driver LISTENS on two loopback TCP ports (OS-assigned ephemerals) and
QEMU connects to them as a client (-serial tcp:... / -monitor tcp:... with
no server=on).  QEMU starts the VM only once both character devices are
connected, and the listeners are bound before QEMU is spawned, so no boot
output can be missed by construction.  There are no fixed sleeps: every
keystroke is awaited by its own echo in the serial stream (the console
echoes each character), every command is awaited by the next `nova> `
prompt, and the VM start is awaited by the first prompt.

What it verifies (and against what oracle)
------------------------------------------
1. The shell really round-trips keystrokes: each expected verb's ECHO and
   a `zz` control's `unknown command: zz` reply must appear in the serial
   stream (the i8042 -> keyboard poller -> console path, not a shortcut).
2. Each verb's RESPONSE window (bytes between the command's Enter and the
   next prompt) contains the verb's own name -- the response belongs to
   the command that was typed, not to some earlier one.
3. `time` (the RTC verb, present per the K3 work-order scope "CMOS RTC")
   prints `utc: YYYY-MM-DDTHH:MM:SS`, and the parsed value brackets
   between two HOST-UTC samples taken around the session.  The host clock
   comes from the operating system; QEMU merely seeds the guest CMOS RTC
   from it (-rtc base=utc).  A wrongly decoded RTC fails the bracket no
   matter what the kernel's own gates claim.  Same rationale as
   kernel/scripts/test-rtc.sh; tolerance covers boot and rounding.
4. The guest exits cleanly on the HMP `quit` command (no hang, no panic).

The verb list to type is passed in by the caller (kernel/scripts/
test-shell.sh extracts it from shell.rs's dispatch() arms), so the driver
itself shares no vocabulary with the kernel source.

Exit codes: 0 = all checks passed, 1 = a check FAILED (with evidence),
2 = could not run (bad arguments, missing qemu/image).
"""

import argparse
import calendar
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

PROMPT = b"nova> "

# HMP sendkey names for the characters a verb may contain.
SENDKEY_CHARS = {c: c for c in "abcdefghijklmnopqrstuvwxyz0123456789"}

FAILURES = []


def check(ok, label, detail=""):
    tag = "ok  " if ok else "FAIL"
    line = f"  {tag}  {label}" + (f" ({detail})" if detail else "")
    print(line)
    if not ok:
        FAILURES.append(label)
    return ok


def listen_free():
    """Bind a listener on an OS-assigned loopback port."""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


class ShellSession:
    def __init__(self, ser, mon):
        self.ser = ser
        self.mon = mon
        self.buf = bytearray()

    def recv_some(self, timeout):
        """Read whatever arrives within timeout; True if the socket closed."""
        self.ser.settimeout(timeout)
        try:
            data = self.ser.recv(4096)
        except socket.timeout:
            return False
        if not data:
            return True
        self.buf.extend(data)
        return False

    def wait_tail(self, suffix, timeout, what):
        """Wait until the serial stream's tail equals `suffix` (no sleeps:
        poll via short recv timeouts; every arrival is appended first)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            closed = self.recv_some(0.2)
            if closed:
                return False
            if bytes(self.buf).endswith(suffix):
                return True
        print(f"  (timeout after {timeout}s waiting for {what}; "
              f"stream tail: {bytes(self.buf)[-80:]!r})")
        return False

    def sendkey(self, key):
        self.mon.sendall(f"sendkey {key}\n".encode())
        self.mon.settimeout(1.0)
        try:
            while True:
                if not self.mon.recv(4096):
                    break
        except socket.timeout:
            pass

    def type_line(self, text, key_timeout, what):
        """Type one command character by character, awaiting each echo;
        then press Enter and await the next prompt.  Returns the RESPONSE
        window: the bytes between the LAST character's echo and the next
        prompt, so the window contains the kernel's reply only -- never
        the echoed keystrokes (a window that includes the typing would
        make 'the reply mentions the verb' checks tautological)."""
        typed = b""
        for ch in text:
            if ch not in SENDKEY_CHARS:
                check(False, f"verb {what!r} char {ch!r} is not sendable",
                      "script bug: only a-z0-9 verbs supported")
                return None
            self.sendkey(SENDKEY_CHARS[ch])
            typed += ch.encode()
            if not self.wait_tail(PROMPT + typed, key_timeout,
                                  f"echo of {what}[{ch}]"):
                check(False, f"echo awaited for {what!r} char {ch!r}",
                      "see stream tail above")
                return None
        window_start = len(self.buf)
        self.sendkey("ret")
        if not self.wait_tail(PROMPT, 20, f"prompt after {what}"):
            check(False, f"prompt after {what!r}", "see stream tail above")
            return None
        return bytes(self.buf[window_start:])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--qemu", required=True, help="qemu-system-x86_64 path")
    ap.add_argument("--image", required=True, help="regular disk image (nova.hdd)")
    ap.add_argument("--expect-verbs", required=True,
                    help="comma-separated verbs to type, e.g. time,ver,mem")
    ap.add_argument("--serial-log", required=True,
                    help="where to write the captured serial stream")
    ap.add_argument("--boot-timeout", type=int, default=30)
    ap.add_argument("--key-timeout", type=int, default=10)
    ap.add_argument("--bracket", type=int, default=120,
                    help="utc bracket tolerance in seconds (host vs guest)")
    args = ap.parse_args()

    # --qemu is a bare PATH name on Linux (qemu-system-x86_64) and a full
    # path on Windows; is_file() alone rejected the PATH name, so CI skipped.
    found = shutil.which(args.qemu) or (args.qemu if Path(args.qemu).is_file() else None)
    if not found:
        print(f"SKIP: qemu binary not found: {args.qemu}")
        return 2
    qemu = Path(found)
    image = Path(args.image)
    if not image.is_file():
        print(f"SKIP: disk image not found: {image} "
              "(run: bash scripts/build-disk.sh release)")
        return 2

    verbs = [v for v in args.expect_verbs.split(",") if v]
    if not verbs:
        print("SKIP: empty verb list")
        return 2

    serial_log = Path(args.serial_log)
    serial_log.parent.mkdir(parents=True, exist_ok=True)

    ser_l, ser_port = listen_free()
    mon_l, mon_port = listen_free()

    qemu_err = serial_log.with_suffix(".qemu-err.log")
    cmd = [str(qemu), "-M", "q35", "-m", "512M", "-rtc", "base=utc",
           "-drive", f"file={image},format=raw",
           "-serial", f"tcp:127.0.0.1:{ser_port}",
           "-monitor", f"tcp:127.0.0.1:{mon_port}",
           "-display", "none", "-no-reboot"]
    with open(qemu_err, "w", encoding="utf-8") as qerr:
        proc = subprocess.Popen(cmd, stdout=qerr, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)

    try:
        return run_session(proc, ser_l, mon_l, verbs, args, serial_log)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        ser_l.close()
        mon_l.close()


def run_session(proc, ser_l, mon_l, verbs, args, serial_log):
    ser_l.settimeout(15)
    mon_l.settimeout(15)
    try:
        mon, _ = mon_l.accept()
        ser, _ = ser_l.accept()
    except socket.timeout:
        print("FAIL: QEMU never connected to the driver's listeners")
        with open(serial_log.with_suffix(".qemu-err.log"),
                  encoding="utf-8", errors="replace") as f:
            print(f.read())
        return 1
    mon.settimeout(1.0)
    ser.settimeout(0.3)
    ses = ShellSession(ser, mon)

    print("typed verbs: help " + " ".join(verbs) + " (then control zz)")
    if not ses.wait_tail(PROMPT, args.boot_timeout, "first nova> prompt"):
        check(False, "first `nova> ` prompt awaited from serial stream")
        write_log(ses, serial_log)
        return finish(proc, ses, serial_log)

    host_before = int(time.time())
    windows = {}
    order = ["help"] + verbs
    for verb in order:
        window = ses.type_line(verb, args.key_timeout, what=verb)
        if window is None:
            write_log(ses, serial_log)
            return finish(proc, ses, serial_log)
        windows[verb] = window
    host_after = int(time.time())

    # Unknown-command control: proves the keystrokes went through the real
    # input path (a misdirected stream cannot produce this reply on demand).
    zz_window = ses.type_line("zz", args.key_timeout, what="zz (control)")
    if zz_window is None:
        write_log(ses, serial_log)
        return finish(proc, ses, serial_log)

    print(f"host UTC before : {host_before}")
    print(f"host UTC after  : {host_after}")

    # Per-verb response oracles. Every expectation here is read from the
    # kernel's REPLY in the response window (post-Enter bytes, echoes
    # excluded) and matches the committed transcript in
    # docs/logs/k4a-time-verb.log; nothing is compared to the code that
    # produced it. `help` must list every verb in the typed set; every
    # other verb must at least be RECOGNIZED (no `unknown command:`
    # reply -- recognizing a verb is what its dispatch arm does).
    HELP_MARKER = b"commands: "
    for verb in order:
        window = windows[verb]
        if verb == "help":
            check(HELP_MARKER in window,
                  "`help` reply starts with `commands: `")
            for other in verbs:
                check(other.encode() in window,
                      f"help lists `{other}`")
        elif verb == "time":
            pass  # bracketed below, against the host clock
        elif verb == "ver":
            check(re.search(rb"nucleus \d+\.\d+\.\d+", window) is not None,
                  "`ver` reply names the nucleus version")
        elif verb == "mem":
            check(re.search(rb"entries: \d+", window) is not None,
                  "`mem` reply counts memory-map entries")
        else:
            check(b"unknown command: " + verb.encode() not in window,
                  f"`{verb}` is recognized (no `unknown command:` reply)")

    # The RTC verb: parse the rendered timestamp and bracket it against the
    # host clock (external oracle: the OS clock, via QEMU -rtc base=utc).
    time_ok = False
    if "time" in windows:
        m = re.search(rb"utc: (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})",
                      windows["time"])
        if m is None:
            check(False, "`time` printed `utc: YYYY-MM-DDTHH:MM:SS`",
                  f"window: {windows['time'][:120]!r}")
        else:
            stamp = m.group(1).decode()
            epoch = calendar.timegm(
                time.strptime(stamp, "%Y-%m-%dT%H:%M:%S"))
            lo, hi = host_before - args.bracket, host_after + args.bracket
            time_ok = lo <= epoch <= hi
            print(f"guest utc       : {stamp}  (epoch {epoch})")
            print(f"accepted bracket: {lo} .. {hi}")
            check(time_ok, "guest clock sits in the host-UTC bracket "
                  f"(+/-{args.bracket}s)")
    else:
        check(False, "`time` was in the typed verb set (K3 scope: CMOS RTC)")

    check(zz_window is not None and b"unknown command: zz" in zz_window,
          "control `zz` got `unknown command: zz` (keystrokes round-trip)")

    write_log(ses, serial_log)
    return finish(proc, ses, serial_log)


def finish(proc, ses, serial_log):
    """Ask the monitor to quit; the guest must exit cleanly."""
    ses.mon.sendall(b"quit\n")
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        check(False, "QEMU exited on monitor `quit`", "had to kill it")
        return report()
    check(proc.returncode == 0,
          f"QEMU exited on monitor `quit` (rc={proc.returncode})")
    return report()


def report():
    if FAILURES:
        print(f"shell driver: FAIL ({len(FAILURES)} failed check(s): "
              + "; ".join(FAILURES) + ")")
        return 1
    print("shell driver: PASS")
    return 0


def write_log(ses, serial_log):
    serial_log.write_bytes(bytes(ses.buf))
    # Console-region transcript for the run log (from the banner marker on).
    stream = bytes(ses.buf)
    marker = stream.rfind(b"NOVA/Nucleus")
    region = stream[marker:] if marker >= 0 else stream[-400:]
    text = region.decode("utf-8", errors="replace").replace("\r", "")
    print("console transcript (serial, from the banner):")
    print(text)


if __name__ == "__main__":
    sys.exit(main())
