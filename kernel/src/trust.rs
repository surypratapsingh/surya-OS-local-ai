//! In-kernel C2 package and manifest verifier (K4).
//!
//! Note on enforcement: no code calls the verifier yet; enforcement in the
//! boot/loader path comes with Ring 3 / install work. The verifier functions
//! are currently exercised and validated by the in-kernel selftest gate.
//!
//! Standard algorithms implemented in pure `no_std` Rust (no crates, AGENTS.md rule 12):
//! - FIPS 180-4 SHA-256 (arbitrary length streaming)
//! - FIPS 180-4 SHA-512 (arbitrary length streaming)
//! - RFC 8032 Ed25519 signature verification (Curve25519)
//! - C2 canonical JSON signing input and package digest verification

use crate::cap::CapSet;

// ---------------------------------------------------------------------------
// 1. SHA-256 (FIPS 180-4)
// ---------------------------------------------------------------------------

const K256: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

pub struct Sha256 {
    h: [u32; 8],
    total_len: u64,
    buf: [u8; 64],
    buf_len: usize,
}

impl Sha256 {
    pub fn new() -> Self {
        Self {
            h: [
                0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
                0x5be0cd19,
            ],
            total_len: 0,
            buf: [0; 64],
            buf_len: 0,
        }
    }

    fn compress(h: &mut [u32; 8], block: &[u8; 64]) {
        let mut w = [0u32; 64];
        for (i, chunk) in block.as_chunks::<4>().0.iter().enumerate() {
            w[i] = u32::from_be_bytes(*chunk);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let (mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh) =
            (h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7]);
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ (!e & g);
            let t1 = hh
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K256[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let t2 = s0.wrapping_add(maj);
            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(t1);
            d = c;
            c = b;
            b = a;
            a = t1.wrapping_add(t2);
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
        h[5] = h[5].wrapping_add(f);
        h[6] = h[6].wrapping_add(g);
        h[7] = h[7].wrapping_add(hh);
    }

    pub fn update(&mut self, mut data: &[u8]) {
        self.total_len += data.len() as u64;
        if self.buf_len > 0 {
            let needed = 64 - self.buf_len;
            if data.len() >= needed {
                self.buf[self.buf_len..64].copy_from_slice(&data[..needed]);
                let block = self.buf;
                Self::compress(&mut self.h, &block);
                self.buf_len = 0;
                data = &data[needed..];
            } else {
                self.buf[self.buf_len..self.buf_len + data.len()].copy_from_slice(data);
                self.buf_len += data.len();
                return;
            }
        }
        while data.len() >= 64 {
            let mut block = [0u8; 64];
            block.copy_from_slice(&data[..64]);
            Self::compress(&mut self.h, &block);
            data = &data[64..];
        }
        if !data.is_empty() {
            self.buf[..data.len()].copy_from_slice(data);
            self.buf_len = data.len();
        }
    }

    pub fn finalize(mut self) -> [u8; 32] {
        let bitlen = self.total_len * 8;
        self.buf[self.buf_len] = 0x80;
        self.buf_len += 1;
        if self.buf_len > 56 {
            for i in self.buf_len..64 {
                self.buf[i] = 0;
            }
            let block = self.buf;
            Self::compress(&mut self.h, &block);
            self.buf_len = 0;
        }
        for i in self.buf_len..56 {
            self.buf[i] = 0;
        }
        self.buf[56..64].copy_from_slice(&bitlen.to_be_bytes());
        let block = self.buf;
        Self::compress(&mut self.h, &block);

        let mut out = [0u8; 32];
        for (i, val) in self.h.iter().enumerate() {
            out[i * 4..(i + 1) * 4].copy_from_slice(&val.to_be_bytes());
        }
        out
    }
}

pub fn sha256(data: &[u8]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(data);
    hasher.finalize()
}

// ---------------------------------------------------------------------------
// 2. SHA-512 (FIPS 180-4)
// ---------------------------------------------------------------------------

const K512: [u64; 80] = [
    0x428a2f98d728ae22,
    0x7137449123ef65cd,
    0xb5c0fbcfec4d3b2f,
    0xe9b5dba58189dbbc,
    0x3956c25bf348b538,
    0x59f111f1b605d019,
    0x923f82a4af194f9b,
    0xab1c5ed5da6d8118,
    0xd807aa98a3030242,
    0x12835b0145706fbe,
    0x243185be4ee4b28c,
    0x550c7dc3d5ffb4e2,
    0x72be5d74f27b896f,
    0x80deb1fe3b1696b1,
    0x9bdc06a725c71235,
    0xc19bf174cf692694,
    0xe49b69c19ef14ad2,
    0xefbe4786384f25e3,
    0x0fc19dc68b8cd5b5,
    0x240ca1cc77ac9c65,
    0x2de92c6f592b0275,
    0x4a7484aa6ea6e483,
    0x5cb0a9dcbd41fbd4,
    0x76f988da831153b5,
    0x983e5152ee66dfab,
    0xa831c66d2db43210,
    0xb00327c898fb213f,
    0xbf597fc7beef0ee4,
    0xc6e00bf33da88fc2,
    0xd5a79147930aa725,
    0x06ca6351e003826f,
    0x142929670a0e6e70,
    0x27b70a8546d22ffc,
    0x2e1b21385c26c926,
    0x4d2c6dfc5ac42aed,
    0x53380d139d95b3df,
    0x650a73548baf63de,
    0x766a0abb3c77b2a8,
    0x81c2c92e47edaee6,
    0x92722c851482353b,
    0xa2bfe8a14cf10364,
    0xa81a664bbc423001,
    0xc24b8b70d0f89791,
    0xc76c51a30654be30,
    0xd192e819d6ef5218,
    0xd69906245565a910,
    0xf40e35855771202a,
    0x106aa07032bbd1b8,
    0x19a4c116b8d2d0c8,
    0x1e376c085141ab53,
    0x2748774cdf8eeb99,
    0x34b0bcb5e19b48a8,
    0x391c0cb3c5c95a63,
    0x4ed8aa4ae3418acb,
    0x5b9cca4f7763e373,
    0x682e6ff3d6b2b8a3,
    0x748f82ee5defb2fc,
    0x78a5636f43172f60,
    0x84c87814a1f0ab72,
    0x8cc702081a6439ec,
    0x90befffa23631e28,
    0xa4506cebde82bde9,
    0xbef9a3f7b2c67915,
    0xc67178f2e372532b,
    0xca273eceea26619c,
    0xd186b8c721c0c207,
    0xeada7dd6cde0eb1e,
    0xf57d4f7fee6ed178,
    0x06f067aa72176fba,
    0x0a637dc5a2c898a6,
    0x113f9804bef90dae,
    0x1b710b35131c471b,
    0x28db77f523047d84,
    0x32caab7b40c72493,
    0x3c9ebe0a15c9bebc,
    0x431d67c49c100d4c,
    0x4cc5d4becb3e42b6,
    0x597f299cfc657e2a,
    0x5fcb6fab3ad6faec,
    0x6c44198c4a475817,
];

pub struct Sha512 {
    h: [u64; 8],
    total_len: u128,
    buf: [u8; 128],
    buf_len: usize,
}

impl Sha512 {
    pub fn new() -> Self {
        Self {
            h: [
                0x6a09e667f3bcc908,
                0xbb67ae8584caa73b,
                0x3c6ef372fe94f82b,
                0xa54ff53a5f1d36f1,
                0x510e527fade682d1,
                0x9b05688c2b3e6c1f,
                0x1f83d9abfb41bd6b,
                0x5be0cd19137e2179,
            ],
            total_len: 0,
            buf: [0; 128],
            buf_len: 0,
        }
    }

    fn compress(h: &mut [u64; 8], block: &[u8; 128]) {
        let mut w = [0u64; 80];
        for (i, chunk) in block.as_chunks::<8>().0.iter().enumerate() {
            w[i] = u64::from_be_bytes(*chunk);
        }
        for i in 16..80 {
            let s0 = w[i - 15].rotate_right(1) ^ w[i - 15].rotate_right(8) ^ (w[i - 15] >> 7);
            let s1 = w[i - 2].rotate_right(19) ^ w[i - 2].rotate_right(61) ^ (w[i - 2] >> 6);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let (mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh) =
            (h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7]);
        for i in 0..80 {
            let s1 = e.rotate_right(14) ^ e.rotate_right(18) ^ e.rotate_right(41);
            let ch = (e & f) ^ (!e & g);
            let t1 = hh
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K512[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(28) ^ a.rotate_right(34) ^ a.rotate_right(39);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let t2 = s0.wrapping_add(maj);
            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(t1);
            d = c;
            c = b;
            b = a;
            a = t1.wrapping_add(t2);
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
        h[5] = h[5].wrapping_add(f);
        h[6] = h[6].wrapping_add(g);
        h[7] = h[7].wrapping_add(hh);
    }

    pub fn update(&mut self, mut data: &[u8]) {
        self.total_len += data.len() as u128;
        if self.buf_len > 0 {
            let needed = 128 - self.buf_len;
            if data.len() >= needed {
                self.buf[self.buf_len..128].copy_from_slice(&data[..needed]);
                let block = self.buf;
                Self::compress(&mut self.h, &block);
                self.buf_len = 0;
                data = &data[needed..];
            } else {
                self.buf[self.buf_len..self.buf_len + data.len()].copy_from_slice(data);
                self.buf_len += data.len();
                return;
            }
        }
        while data.len() >= 128 {
            let mut block = [0u8; 128];
            block.copy_from_slice(&data[..128]);
            Self::compress(&mut self.h, &block);
            data = &data[128..];
        }
        if !data.is_empty() {
            self.buf[..data.len()].copy_from_slice(data);
            self.buf_len = data.len();
        }
    }

    pub fn finalize(mut self) -> [u8; 64] {
        let bitlen = self.total_len * 8;
        self.buf[self.buf_len] = 0x80;
        self.buf_len += 1;
        if self.buf_len > 112 {
            for i in self.buf_len..128 {
                self.buf[i] = 0;
            }
            let block = self.buf;
            Self::compress(&mut self.h, &block);
            self.buf_len = 0;
        }
        for i in self.buf_len..112 {
            self.buf[i] = 0;
        }
        self.buf[112..128].copy_from_slice(&bitlen.to_be_bytes());
        let block = self.buf;
        Self::compress(&mut self.h, &block);

        let mut out = [0u8; 64];
        for (i, val) in self.h.iter().enumerate() {
            out[i * 8..(i + 1) * 8].copy_from_slice(&val.to_be_bytes());
        }
        out
    }
}

pub fn sha512(data: &[u8]) -> [u8; 64] {
    let mut hasher = Sha512::new();
    hasher.update(data);
    hasher.finalize()
}

// ---------------------------------------------------------------------------
// 3. Curve25519 & Ed25519 (RFC 8032)
// ---------------------------------------------------------------------------

/// 256-bit unsigned integer represented in 4 little-endian `u64` words.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
struct U256(pub [u64; 4]);

const P_WORDS: [u64; 4] = [
    0xFFFF_FFFF_FFFF_FFED,
    0xFFFF_FFFF_FFFF_FFFF,
    0xFFFF_FFFF_FFFF_FFFF,
    0x7FFF_FFFF_FFFF_FFFF,
];

// RFC 8032 §5.1: Group order L = 2^252 + 27742317777372353535851937790883648493
const L_WORDS: [u64; 4] = [
    0x5812_631a_5cf5_d3ed,
    0x14de_f9de_a2f7_9cd6,
    0x0000_0000_0000_0000,
    0x1000_0000_0000_0000,
];

// d = -121665 * inv(121666) mod p
const D_WORDS: [u64; 4] = [
    0x75eb_4dca_1359_78a3,
    0x0070_0a4d_4141_d8ab,
    0x8cc7_4079_7779_e898,
    0x5203_6cee_2b6f_fe73,
];

// Base point By = 4/5 mod p
const BY_WORDS: [u64; 4] = [
    0x6666_6666_6666_6658,
    0x6666_6666_6666_6666,
    0x6666_6666_6666_6666,
    0x6666_6666_6666_6666,
];

// Base point Bx = even root
const BX_WORDS: [u64; 4] = [
    0xc956_2d60_8f25_d51a,
    0x692c_c760_9525_a7b2,
    0xc0a4_e231_fdd6_dc5c,
    0x2169_36d3_cd6e_53fe,
];

// sqrt(-1) mod p = 2^((p-1)/4) mod p
const SQRT_M1_WORDS: [u64; 4] = [
    0xc4ee_1b27_4a0e_a0b0,
    0x2f43_1806_ad2f_e478,
    0x2b4d_0099_3dfb_d7a7,
    0x2b83_2480_4fc1_df0b,
];

impl U256 {
    pub const ZERO: Self = Self([0, 0, 0, 0]);
    pub const ONE: Self = Self([1, 0, 0, 0]);

    pub fn from_bytes(b: &[u8; 32]) -> Self {
        Self([
            u64::from_le_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]]),
            u64::from_le_bytes([b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15]]),
            u64::from_le_bytes([b[16], b[17], b[18], b[19], b[20], b[21], b[22], b[23]]),
            u64::from_le_bytes([b[24], b[25], b[26], b[27], b[28], b[29], b[30], b[31]]),
        ])
    }

    pub fn is_zero(&self) -> bool {
        self.0[0] == 0 && self.0[1] == 0 && self.0[2] == 0 && self.0[3] == 0
    }

    pub fn gte(&self, other: &Self) -> bool {
        for i in (0..4).rev() {
            if self.0[i] > other.0[i] {
                return true;
            }
            if self.0[i] < other.0[i] {
                return false;
            }
        }
        true
    }

    pub fn add(&self, other: &Self) -> (Self, bool) {
        let mut res = [0u64; 4];
        let mut carry = 0u64;
        for (i, item) in res.iter_mut().enumerate() {
            let sum = (self.0[i] as u128) + (other.0[i] as u128) + (carry as u128);
            *item = sum as u64;
            carry = (sum >> 64) as u64;
        }
        (Self(res), carry != 0)
    }

    pub fn sub(&self, other: &Self) -> (Self, bool) {
        let mut res = [0u64; 4];
        let mut borrow = 0i128;
        for (i, item) in res.iter_mut().enumerate() {
            let diff = (self.0[i] as i128) - (other.0[i] as i128) - borrow;
            if diff < 0 {
                *item = (diff + (1i128 << 64)) as u64;
                borrow = 1;
            } else {
                *item = diff as u64;
                borrow = 0;
            }
        }
        (Self(res), borrow != 0)
    }

    pub fn add_mod_p(&self, other: &Self) -> Self {
        let p = Self(P_WORDS);
        let (s, carry) = self.add(other);
        if carry || s.gte(&p) {
            let (diff, _) = s.sub(&p);
            diff
        } else {
            s
        }
    }

    pub fn sub_mod_p(&self, other: &Self) -> Self {
        let p = Self(P_WORDS);
        if self.gte(other) {
            let (diff, _) = self.sub(other);
            diff
        } else {
            let (sum, _) = self.add(&p);
            let (diff, _) = sum.sub(other);
            diff
        }
    }

    /// Multiply two U256 numbers producing an 8-limb U512, then reduce modulo p.
    pub fn mul_mod_p(&self, other: &Self) -> Self {
        let mut wide = [0u64; 8];
        for i in 0..4 {
            let mut carry = 0u128;
            for j in 0..4 {
                let cur =
                    (self.0[i] as u128) * (other.0[j] as u128) + (wide[i + j] as u128) + carry;
                wide[i + j] = cur as u64;
                carry = cur >> 64;
            }
            let mut k = i + 4;
            while carry > 0 && k < 8 {
                let cur = (wide[k] as u128) + carry;
                wide[k] = cur as u64;
                carry = cur >> 64;
                k += 1;
            }
        }
        Self::reduce_wide_p(&wide)
    }

    fn reduce_wide_p(wide: &[u64; 8]) -> Self {
        let p = Self(P_WORDS);

        // Step 1: Split wide (512 bits) into low 255 bits (cur) and high 257 bits (high).
        let cur = [wide[0], wide[1], wide[2], wide[3] & 0x7FFF_FFFF_FFFF_FFFF];
        let high = [
            (wide[3] >> 63) | (wide[4] << 1),
            (wide[4] >> 63) | (wide[5] << 1),
            (wide[5] >> 63) | (wide[6] << 1),
            (wide[6] >> 63) | (wide[7] << 1),
            wide[7] >> 63,
        ];

        // Since 2^255 = 19 (mod p), wide = cur + high * 19 (mod p).
        // Multiply high by 19:
        let mut prod = [0u64; 6];
        let mut carry = 0u128;
        for i in 0..5 {
            let m = (high[i] as u128) * 19 + carry;
            prod[i] = m as u64;
            carry = m >> 64;
        }
        prod[5] = carry as u64;

        // m_limbs = cur + prod:
        let mut m_limbs = [0u64; 6];
        let mut carry = 0u128;
        for i in 0..4 {
            let s = (cur[i] as u128) + (prod[i] as u128) + carry;
            m_limbs[i] = s as u64;
            carry = s >> 64;
        }
        for i in 4..6 {
            let s = (prod[i] as u128) + carry;
            m_limbs[i] = s as u64;
            carry = s >> 64;
        }

        // Step 2: Split m_limbs into low 255 bits (cur2) and high bits (high2).
        let cur2 = [
            m_limbs[0],
            m_limbs[1],
            m_limbs[2],
            m_limbs[3] & 0x7FFF_FFFF_FFFF_FFFF,
        ];
        let high2 = [
            (m_limbs[3] >> 63) | (m_limbs[4] << 1),
            (m_limbs[4] >> 63) | (m_limbs[5] << 1),
        ];

        // Multiply high2 by 19:
        let mut h2_prod = [0u64; 3];
        let mut carry = 0u128;
        for i in 0..2 {
            let val = (high2[i] as u128) * 19 + carry;
            h2_prod[i] = val as u64;
            carry = val >> 64;
        }
        h2_prod[2] = carry as u64;

        // res = cur2 + h2_prod:
        let mut res = [0u64; 4];
        let mut carry = 0u128;
        for i in 0..4 {
            let p_val = if i < 3 { h2_prod[i] as u128 } else { 0 };
            let s = (cur2[i] as u128) + p_val + carry;
            res[i] = s as u64;
            carry = s >> 64;
        }

        // Step 3: Fold bit 255 and any carry out:
        let top_bit = (res[3] >> 63) + (carry as u64 * 2);
        res[3] &= 0x7FFF_FFFF_FFFF_FFFF;
        let mut final_carry = (top_bit as u128) * 19;
        for item in &mut res {
            let s = (*item as u128) + final_carry;
            *item = s as u64;
            final_carry = s >> 64;
        }

        let mut final_val = Self(res);
        while final_val.gte(&p) {
            let (diff, _) = final_val.sub(&p);
            final_val = diff;
        }
        final_val
    }

    pub fn pow_mod_p(&self, exp: &Self) -> Self {
        let mut res = Self::ONE;
        let mut base = *self;
        for i in 0..4 {
            let mut word = exp.0[i];
            for _ in 0..64 {
                if (word & 1) != 0 {
                    res = res.mul_mod_p(&base);
                }
                base = base.mul_mod_p(&base);
                word >>= 1;
            }
        }
        res
    }

    pub fn inv_mod_p(&self) -> Self {
        // Fermat's Little Theorem: a^(p-2) mod p
        // p - 2 = 2^255 - 21 = 0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffeb
        let p_minus_2 = Self([
            0xFFFF_FFFF_FFFF_FFEB,
            0xFFFF_FFFF_FFFF_FFFF,
            0xFFFF_FFFF_FFFF_FFFF,
            0x7FFF_FFFF_FFFF_FFFF,
        ]);
        self.pow_mod_p(&p_minus_2)
    }
}

/// Extended homogeneous coordinates (X, Y, Z, T) with x = X/Z, y = Y/Z, x*y = T/Z.
#[derive(Clone, Copy, Debug)]
struct Point {
    x: U256,
    y: U256,
    z: U256,
    t: U256,
}

impl Point {
    pub const NEUTRAL: Self = Self {
        x: U256::ZERO,
        y: U256::ONE,
        z: U256::ONE,
        t: U256::ZERO,
    };

    pub fn add(&self, other: &Self) -> Self {
        let d = U256(D_WORDS);
        // RFC 8032 §5.1.4 addition formula
        let a = self
            .y
            .sub_mod_p(&self.x)
            .mul_mod_p(&other.y.sub_mod_p(&other.x));
        let b = self
            .y
            .add_mod_p(&self.x)
            .mul_mod_p(&other.y.add_mod_p(&other.x));
        let c = self.t.mul_mod_p(&other.t).mul_mod_p(&d);
        let c2 = c.add_mod_p(&c);
        let d_val = self.z.mul_mod_p(&other.z);
        let d2 = d_val.add_mod_p(&d_val);
        let e = b.sub_mod_p(&a);
        let f = d2.sub_mod_p(&c2);
        let g = d2.add_mod_p(&c2);
        let h = b.add_mod_p(&a);
        Self {
            x: e.mul_mod_p(&f),
            y: g.mul_mod_p(&h),
            z: f.mul_mod_p(&g),
            t: e.mul_mod_p(&h),
        }
    }

    pub fn scalar_mul(&self, s: &U256) -> Self {
        let mut res = Self::NEUTRAL;
        let mut base = *self;
        for i in 0..4 {
            let mut word = s.0[i];
            for _ in 0..64 {
                if (word & 1) != 0 {
                    res = res.add(&base);
                }
                base = base.add(&base);
                word >>= 1;
            }
        }
        res
    }

    pub fn equals(&self, other: &Self) -> bool {
        let x1_z2 = self.x.mul_mod_p(&other.z);
        let x2_z1 = other.x.mul_mod_p(&self.z);
        let y1_z2 = self.y.mul_mod_p(&other.z);
        let y2_z1 = other.y.mul_mod_p(&self.z);
        x1_z2 == x2_z1 && y1_z2 == y2_z1
    }
}

/// Recover x from y and sign bit (RFC 8032 §5.1.3).
fn recover_x(y: &U256, sign: u8) -> Option<U256> {
    let p = U256(P_WORDS);
    if y.gte(&p) {
        return None;
    }
    let d = U256(D_WORDS);
    let y2 = y.mul_mod_p(y);
    let u = y2.sub_mod_p(&U256::ONE);
    let v = d.mul_mod_p(&y2).add_mod_p(&U256::ONE);
    if v.is_zero() {
        return if u.is_zero() {
            if sign == 0 {
                Some(U256::ZERO)
            } else {
                None
            }
        } else {
            None
        };
    }
    // x2 = u * inv(v)
    let v_inv = v.inv_mod_p();
    let x2 = u.mul_mod_p(&v_inv);
    if x2.is_zero() {
        return if sign == 0 { Some(U256::ZERO) } else { None };
    }
    // x = x2^((p+3)/8) mod p
    // (p+3)/8 = 2^252 - 2
    let p_plus_3_div_8 = U256([
        0xFFFF_FFFF_FFFF_FFFE,
        0xFFFF_FFFF_FFFF_FFFF,
        0xFFFF_FFFF_FFFF_FFFF,
        0x0FFF_FFFF_FFFF_FFFF,
    ]);
    let mut x = x2.pow_mod_p(&p_plus_3_div_8);
    let check = x.mul_mod_p(&x);
    if check != x2 {
        let sqrt_m1 = U256(SQRT_M1_WORDS);
        x = x.mul_mod_p(&sqrt_m1);
        let check2 = x.mul_mod_p(&x);
        if check2 != x2 {
            return None;
        }
    }
    if (x.0[0] & 1) as u8 != sign {
        x = p.sub_mod_p(&x);
    }
    Some(x)
}

fn decode_point(bytes: &[u8; 32]) -> Option<Point> {
    let mut y_bytes = *bytes;
    let sign = (y_bytes[31] >> 7) & 1;
    y_bytes[31] &= 0x7F;
    let y = U256::from_bytes(&y_bytes);
    let x = recover_x(&y, sign)?;
    let t = x.mul_mod_p(&y);
    Some(Point {
        x,
        y,
        z: U256::ONE,
        t,
    })
}

/// Reduce a 512-bit integer modulo L using bitwise division.
fn reduce_512_mod_l(h: &[u8; 64]) -> U256 {
    let l = U256(L_WORDS);
    let mut rem = U256::ZERO;
    for bit_idx in (0..512).rev() {
        let byte_pos = bit_idx / 8;
        let bit_pos = bit_idx % 8;
        let bit = (h[byte_pos] >> bit_pos) & 1;

        // rem = (rem << 1) | bit
        let mut new_rem = [0u64; 4];
        let mut carry = bit as u64;
        for (i, item) in new_rem.iter_mut().enumerate() {
            let next_carry = rem.0[i] >> 63;
            *item = (rem.0[i] << 1) | carry;
            carry = next_carry;
        }
        let mut rem_val = U256(new_rem);
        if carry != 0 || rem_val.gte(&l) {
            let (diff, _) = rem_val.sub(&l);
            rem_val = diff;
        }
        rem = rem_val;
    }
    rem
}

/// Verify an Ed25519 signature over a message with a given public key.
pub fn ed25519_verify(public_key: &[u8; 32], message: &[u8], signature: &[u8; 64]) -> bool {
    let a_point = match decode_point(public_key) {
        Some(p) => p,
        None => return false,
    };
    let mut r_bytes = [0u8; 32];
    r_bytes.copy_from_slice(&signature[..32]);
    let r_point = match decode_point(&r_bytes) {
        Some(p) => p,
        None => return false,
    };
    let mut s_bytes = [0u8; 32];
    s_bytes.copy_from_slice(&signature[32..]);
    let s = U256::from_bytes(&s_bytes);
    let l = U256(L_WORDS);
    if s.gte(&l) {
        return false; // non-canonical S (malleability rejection)
    }

    // k = SHA-512(R || A || message) mod L
    let mut hasher = Sha512::new();
    hasher.update(&r_bytes);
    hasher.update(public_key);
    hasher.update(message);
    let h512 = hasher.finalize();
    let k = reduce_512_mod_l(&h512);

    // Compute [S]B
    let b_point = Point {
        x: U256(BX_WORDS),
        y: U256(BY_WORDS),
        z: U256::ONE,
        t: U256(BX_WORDS).mul_mod_p(&U256(BY_WORDS)),
    };
    let sb = b_point.scalar_mul(&s);

    // Compute R + [k]A
    let ka = a_point.scalar_mul(&k);
    let r_plus_ka = r_point.add(&ka);

    sb.equals(&r_plus_ka)
}

// ---------------------------------------------------------------------------
// 4. In-Kernel Package & Manifest Verifier
// ---------------------------------------------------------------------------

pub const SIGNING_CONTEXT: &[u8] = b"NOVA-MANIFEST-v1\n";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TrustError {
    InvalidJson,
    VersionMismatch,
    BadAlgorithm,
    BadFingerprint,
    SignatureRejected,
    DigestMismatch,
    UnknownCapability,
}

pub struct VerifiedPackage {
    pub id: &'static str,
    pub filename: &'static str,
    pub sha256: [u8; 32],
    pub caps: CapSet,
}

/// Verify a package ELF payload against its expected SHA-256 digest.
pub fn verify_package_elf(elf_bytes: &[u8], expected_sha256: &[u8; 32]) -> bool {
    let actual = sha256(elf_bytes);
    actual == *expected_sha256
}

/// Verify an Ed25519-signed manifest against the embedded root authority.
pub fn verify_manifest_signature(
    root_public_key: &[u8; 32],
    canonical_unsigned_json: &[u8],
    signature: &[u8; 64],
) -> bool {
    let mut signing_input = [0u8; 1024];
    let ctx_len = SIGNING_CONTEXT.len();
    if ctx_len + canonical_unsigned_json.len() > signing_input.len() {
        return false;
    }
    signing_input[..ctx_len].copy_from_slice(SIGNING_CONTEXT);
    signing_input[ctx_len..ctx_len + canonical_unsigned_json.len()]
        .copy_from_slice(canonical_unsigned_json);
    let msg = &signing_input[..ctx_len + canonical_unsigned_json.len()];
    ed25519_verify(root_public_key, msg, signature)
}
