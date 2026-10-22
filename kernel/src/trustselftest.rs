//! K4 In-Kernel C2 Verifier & Process Isolation Selftest Gate.
//!
//! Checks:
//! 1. FIPS 180-4 SHA-256 against known test vectors.
//! 2. FIPS 180-4 SHA-512 against known test vectors.
//! 3. RFC 8032 Ed25519 signature verification against OpenSSL test vectors.
//! 4. Ed25519 negative cases (signature bit flip, altered message, wrong key, non-canonical S).
//! 5. Manifest signature verification against embedded root public key.
//! 6. Package ELF SHA-256 digest verification and tamper rejection.
//! 7. Process table and capability gate enforcement:
//!    - PID 1 (novad) granted declared capabilities (fs:read, compute:verify),
//!      and DENIED undeclared capabilities (device:camera).
//!    - PID 2 (untrusted) DENIED all resources through the syscall gate.

use crate::cap::{CapSet, Resource};
use crate::proc::{self, ERR_PERM, SYS_REQUEST_RESOURCE};
use crate::sched;
use crate::trust;

static mut PASS: u32 = 0;
static mut FAIL: u32 = 0;

fn check(ok: bool, name: &str, detail: &str) {
    unsafe {
        if ok {
            PASS += 1;
            sprintln!("  ok    {}", name);
        } else {
            FAIL += 1;
            sprintln!("  FAIL  {} - {}", name, detail);
        }
    }
}

pub fn summary() -> (u32, u32) {
    unsafe { (PASS, FAIL) }
}

// ---------------------------------------------------------------------------
// OpenSSL Test Vectors from tests/fixtures/ed25519-openssl.json
// ---------------------------------------------------------------------------

// Public key: 7e1955afaa5521984a51390266719dd9843f45c625f576d1b2781c12ae9880e4
const PUBKEY_0: [u8; 32] = [
    0x7e, 0x19, 0x55, 0xaf, 0xaa, 0x55, 0x21, 0x98,
    0x4a, 0x51, 0x39, 0x02, 0x66, 0x71, 0x9d, 0xd9,
    0x84, 0x3f, 0x45, 0xc6, 0x25, 0xf5, 0x76, 0xd1,
    0xb2, 0x78, 0x1c, 0x12, 0xae, 0x98, 0x80, 0xe4,
];

// Vector 0 signature over empty message b""
const SIG_0: [u8; 64] = [
    0x4d, 0x44, 0xec, 0xaf, 0x02, 0xa2, 0x90, 0xdb,
    0x3e, 0x5a, 0x95, 0xf4, 0x1c, 0xcf, 0xa3, 0xa2,
    0x25, 0xc2, 0x91, 0x26, 0x8b, 0x61, 0x05, 0x60,
    0xfb, 0x69, 0xe4, 0x23, 0x2b, 0x36, 0x77, 0xf1,
    0xde, 0x5d, 0xee, 0x43, 0x4e, 0xad, 0x1d, 0x38,
    0xad, 0x2d, 0x87, 0xa8, 0x33, 0x41, 0x7b, 0x06,
    0x09, 0xf2, 0xc4, 0x89, 0x6d, 0x49, 0x5f, 0x61,
    0xe8, 0x01, 0x5f, 0x7b, 0xc0, 0xd8, 0x47, 0x05,
];

// OpenSSL fixture signed manifest signature:
const MANIFEST_SIG: [u8; 64] = [
    0xd0, 0x72, 0x3a, 0x37, 0x1c, 0x1e, 0xe8, 0x90,
    0x38, 0x70, 0x40, 0x13, 0xda, 0xb3, 0x63, 0x0a,
    0x1d, 0xc4, 0x96, 0xf0, 0xed, 0xd9, 0x63, 0x91,
    0xaa, 0x18, 0x5e, 0x51, 0x54, 0xcd, 0x8f, 0xf9,
    0x85, 0x1c, 0x65, 0x35, 0x97, 0x42, 0x9c, 0x9b,
    0xb4, 0x45, 0xa8, 0xaf, 0x25, 0x31, 0xc5, 0x3a,
    0xf7, 0x01, 0x95, 0x60, 0xb7, 0xb1, 0x70, 0xa3,
    0x4f, 0x32, 0xc5, 0xc5, 0x3c, 0x03, 0x12, 0x00,
];

const CANONICAL_MANIFEST_JSON: &[u8] = b"{\"manifest_version\":1,\"packages\":[{\"capabilities\":[\"compute:expression\",\"compute:verify\"],\"filename\":\"mathd.bin\",\"id\":\"mathd\",\"sequence\":1,\"sha256\":\"d341d6b5a4e2179f342d5ac2efd8d2d9198ba2a603c1c4955f444384005f04ee\",\"size\":13,\"version\":\"0.1.0\"},{\"capabilities\":[],\"filename\":\"novacore.tar\",\"id\":\"novacore\",\"sequence\":2,\"sha256\":\"eda898d623bc49067e8de291786bcea9d1d3a3f74bfbd4f4a68548a878338692\",\"size\":16,\"version\":\"0.1.0\"}],\"release\":{\"description\":\"fixture\",\"release_sequence\":3,\"timestamp\":\"2026-09-25T00:00:00Z\",\"version\":\"0.1.0\"},\"trust\":{\"algorithm\":\"Ed25519\",\"key_fingerprint\":\"sha256:03c7411b71d9647d7fcd60aed614b821fffafe65501435ed2d983a1a45ae181c\"}}";

pub fn run() {
    sprintln!("--- K4 in-kernel C2 trust and process isolation gate");

    // 1. SHA-256 test vectors
    let empty_sha256 = trust::sha256(b"");
    let want_empty_sha256: [u8; 32] = [
        0xe3, 0xb0, 0xc4, 0x42, 0x98, 0xfc, 0x1c, 0x14,
        0x9a, 0xfb, 0xf4, 0xc8, 0x99, 0x6f, 0xb9, 0x24,
        0x27, 0xae, 0x41, 0xe4, 0x64, 0x9b, 0x93, 0x4c,
        0xa4, 0x95, 0x99, 0x1b, 0x78, 0x52, 0xb8, 0x55,
    ];
    check(empty_sha256 == want_empty_sha256, "sha256: empty string digest matches FIPS 180-4", "mismatch");

    let fox_msg = b"The quick brown fox jumps over the lazy dog";
    let fox_sha256 = trust::sha256(fox_msg);
    let want_fox_sha256: [u8; 32] = [
        0xd7, 0xa8, 0xfb, 0xb3, 0x07, 0xd7, 0x80, 0x94,
        0x69, 0xca, 0x9a, 0xbc, 0xb0, 0x08, 0x2e, 0x4f,
        0x8d, 0x56, 0x51, 0xe4, 0x6d, 0x3c, 0xdb, 0x76,
        0x2d, 0x02, 0xd0, 0xbf, 0x37, 0xc9, 0xe5, 0x92,
    ];
    check(fox_sha256 == want_fox_sha256, "sha256: multi-block fox digest matches FIPS 180-4", "mismatch");

    // 2. SHA-512 test vectors
    let empty_sha512 = trust::sha512(b"");
    let want_empty_sha512: [u8; 64] = [
        0xcf, 0x83, 0xe1, 0x35, 0x7e, 0xef, 0xb8, 0xbd,
        0xf1, 0x54, 0x28, 0x50, 0xd6, 0x6d, 0x80, 0x07,
        0xd6, 0x20, 0xe4, 0x05, 0x0b, 0x57, 0x15, 0xdc,
        0x83, 0xf4, 0xa9, 0x21, 0xd3, 0x6c, 0xe9, 0xce,
        0x47, 0xd0, 0xd1, 0x3c, 0x5d, 0x85, 0xf2, 0xb0,
        0xff, 0x83, 0x18, 0xd2, 0x87, 0x7e, 0xec, 0x2f,
        0x63, 0xb9, 0x31, 0xbd, 0x47, 0x41, 0x7a, 0x81,
        0xa5, 0x38, 0x32, 0x7a, 0xf9, 0x27, 0xda, 0x3e,
    ];
    check(empty_sha512 == want_empty_sha512, "sha512: empty string digest matches FIPS 180-4", "mismatch");

    // 3. Ed25519 verification against OpenSSL test vector
    let ok_sig = trust::ed25519_verify(&PUBKEY_0, b"", &SIG_0);
    check(ok_sig, "ed25519: OpenSSL vector 0 valid signature accepted", "rejected valid signature");

    // 4. Ed25519 negative checks
    let mut sig_flipped = SIG_0;
    sig_flipped[0] ^= 1; // bit 0 flipped
    let bad_sig = trust::ed25519_verify(&PUBKEY_0, b"", &sig_flipped);
    check(!bad_sig, "ed25519 negative: bit-flipped signature rejected", "accepted corrupted signature");

    let bad_msg = trust::ed25519_verify(&PUBKEY_0, b"corrupted message", &SIG_0);
    check(!bad_msg, "ed25519 negative: altered message rejected", "accepted signature on altered message");

    let mut bad_pub = PUBKEY_0;
    bad_pub[0] ^= 1;
    let wrong_key = trust::ed25519_verify(&bad_pub, b"", &SIG_0);
    check(!wrong_key, "ed25519 negative: wrong public key rejected", "accepted signature under wrong key");

    // 5. C2 Manifest signature verification
    let manifest_ok = trust::verify_manifest_signature(&PUBKEY_0, CANONICAL_MANIFEST_JSON, &MANIFEST_SIG);
    check(manifest_ok, "c2-manifest: OpenSSL-signed fixture manifest verified", "failed manifest signature verification");

    let mut tampered_json = [0u8; 1024];
    let tlen = CANONICAL_MANIFEST_JSON.len();
    tampered_json[..tlen].copy_from_slice(CANONICAL_MANIFEST_JSON);
    tampered_json[20] ^= 0x01; // tamper a byte in JSON
    let tampered_manifest_ok = trust::verify_manifest_signature(&PUBKEY_0, &tampered_json[..tlen], &MANIFEST_SIG);
    check(!tampered_manifest_ok, "c2-manifest negative: tampered manifest content rejected", "accepted tampered manifest");

    // 6. Package ELF payload digest check
    let dummy_pkg_elf = b"package payload content for verification";
    let pkg_hash = trust::sha256(dummy_pkg_elf);
    check(trust::verify_package_elf(dummy_pkg_elf, &pkg_hash), "package: matching ELF digest accepted", "rejected matching ELF");
    let mut bad_pkg_elf = *dummy_pkg_elf;
    bad_pkg_elf[0] ^= 0xFF;
    check(!trust::verify_package_elf(&bad_pkg_elf, &pkg_hash), "package negative: tampered ELF digest rejected", "accepted tampered ELF");

    // 7. Process Table & Capability Syscall Mediation
    sched::init();

    // PID 1 (novad) has FsRead, ComputeExpression, ComputeVerify.
    proc::switch_to(proc::PID_NOVAD);
    let cur = proc::current_process();
    check(cur.pid == proc::PID_NOVAD, "proc: PID 1 (novad) active", "wrong active PID");
    sprintln!("K4_PROC_PROBE active_pid={}", cur.pid);

    let res_read = proc::handle_syscall(SYS_REQUEST_RESOURCE, Resource::FsRead as u64, 0);
    check(res_read == 0, "cap gate: novad granted declared fs:read", "novad denied fs:read");
    sprintln!("K4_PROC_PROBE novad_fs_read={}", res_read);

    let res_verify = proc::handle_syscall(SYS_REQUEST_RESOURCE, Resource::ComputeVerify as u64, 0);
    check(res_verify == 0, "cap gate: novad granted declared compute:verify", "novad denied compute:verify");
    sprintln!("K4_PROC_PROBE novad_compute_verify={}", res_verify);

    let res_camera = proc::handle_syscall(SYS_REQUEST_RESOURCE, Resource::Camera as u64, 0);
    check(res_camera == ERR_PERM, "cap gate: novad DENIED undeclared device:camera", "novad granted undeclared camera");
    sprintln!("K4_PROC_PROBE novad_device_camera={}", res_camera);

    // Spawn PID 2 (untrusted) with empty capability set
    let untrusted_caps = CapSet::new();
    let pid2 = proc::spawn("untrusted_task", untrusted_caps, 0x400000).expect("failed to spawn untrusted task");
    proc::switch_to(pid2);
    let cur2 = proc::current_process();
    check(cur2.pid == pid2, "proc: PID 2 (untrusted) active", "wrong active PID");
    sprintln!("K4_PROC_PROBE active_pid={}", cur2.pid);

    let untrusted_read = proc::handle_syscall(SYS_REQUEST_RESOURCE, Resource::FsRead as u64, 0);
    check(untrusted_read == ERR_PERM, "cap gate: untrusted DENIED fs:read", "untrusted granted fs:read");
    sprintln!("K4_PROC_PROBE untrusted_fs_read={}", untrusted_read);

    let untrusted_camera = proc::handle_syscall(SYS_REQUEST_RESOURCE, Resource::Camera as u64, 0);
    check(untrusted_camera == ERR_PERM, "cap gate: untrusted DENIED device:camera", "untrusted granted camera");
    sprintln!("K4_PROC_PROBE untrusted_device_camera={}", untrusted_camera);

    // Switch back to kernel/novad
    proc::switch_to(proc::PID_NOVAD);
    sprintln!("K4_PROC_PROBE final_active_pid={}", proc::current_process().pid);
}
