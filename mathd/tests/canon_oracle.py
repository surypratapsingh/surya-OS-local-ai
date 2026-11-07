#!/usr/bin/env python3
"""Independent specification oracle for canonical serialization and FNV-1a 64-bit hashing.

Specification citations:
1. FNV-1a 64-bit Hash Algorithm:
   - Specification: Fowler-Noll-Vo hash function (IETF draft-eastlake-fnv-17)
   - Initial offset basis: 0xcbf29ce484222325 (14695981039346656037)
   - FNV prime: 0x100000001b3 (1099511628211)
   - Algorithm:
       hash = offset_basis
       for each octet_of_data:
           hash = hash XOR octet_of_data
           hash = (hash * FNV_prime) mod 2^64
   - Standard reference test vectors:
       ""       -> 0xcbf29ce484222325
       "a"      -> 0xaf63dc4c8601ec8c
       "foobar" -> 0x85944171f73967e8

2. Canonical serialization format:
   - Specification: mathd/src/canonicalizer.rs lines 673-736 (serialize function)
     and lines 57-63 (structural_hash specification):
     * Integer(v):
         Tag 'I', base-10 string of v, followed by ';' (lines 688-692)
     * Decimal(v):
         Tag 'D', base-10 string of IEEE 754 64-bit binary representation bits, followed by ';' (lines 693-699)
     * Constant(c):
         Tag 'C', name ("pi", "e"), followed by ';' (lines 700-704)
     * Variable(name):
         Tag 'V', identifier string, followed by ';' (lines 705-709)
     * Call(func, [arg]):
         Tag 'F', function name, '(', serialized argument, ')' (lines 710-718)
     * UnaryOp(op, operand):
         Tag 'U', symbol ("-", "+"), '(', serialized operand, ')' (lines 719-725)
     * BinOp(op, left, right):
         Tag 'B', symbol ("+", "-", "*", "/", "^"), '(', serialized left, ',', serialized right, ')' (lines 726-735)

This script is stdlib-only and independent of any Rust implementation.
"""

import struct
import sys

FNV1A_64_OFFSET_BASIS = 0xcbf29ce484222325
FNV1A_64_PRIME = 0x100000001b3
MASK_64 = 0xffffffffffffffff

def fnv1a_64(data: bytes) -> int:
    """Compute 64-bit FNV-1a hash according to standard specification."""
    h = FNV1A_64_OFFSET_BASIS
    for b in data:
        h ^= b
        h = (h * FNV1A_64_PRIME) & MASK_64
    return h

def fnv1a_64_mutant_add(data: bytes) -> int:
    """Mutant M37: replace XOR with wrapping add."""
    h = FNV1A_64_OFFSET_BASIS
    for b in data:
        h = (h + b) & MASK_64
        h = (h * FNV1A_64_PRIME) & MASK_64
    return h

def fnv1a_64_mutant_basis(data: bytes) -> int:
    """Mutant M38: mutate initial offset basis (+1)."""
    h = 0xcbf29ce484222326
    for b in data:
        h ^= b
        h = (h * FNV1A_64_PRIME) & MASK_64
    return h

def f64_to_bits(val: float) -> int:
    """Return IEEE 754 64-bit integer bit pattern (equivalent to Rust f64::to_bits)."""
    return struct.unpack(">Q", struct.pack(">d", val))[0]

def serialize_int(v: int) -> str:
    return f"I{v};"

def serialize_dec(v: float) -> str:
    return f"D{f64_to_bits(v)};"

def serialize_const(name: str) -> str:
    return f"C{name};"

def serialize_var(name: str) -> str:
    return f"V{name};"

def serialize_call(func: str, arg: str) -> str:
    return f"F{func}({arg})"

def serialize_unary(op: str, operand: str) -> str:
    return f"U{op}({operand})"

def serialize_binop(op: str, left: str, right: str) -> str:
    return f"B{op}({left},{right})"

def main():
    print("=" * 70)
    print("B1/B5/A1.14 INDEPENDENT CANONICAL HASH ORACLE (SPEC SPECIFICATION)")
    print("=" * 70)

    # 1. Published FNV-1a Reference Vectors
    print("\n--- 1. Published FNV-1a 64-bit Reference Vectors ---")
    std_vectors = [
        (b"", "empty string", 0xcbf29ce484222325),
        (b"a", "character 'a'", 0xaf63dc4c8601ec8c),
        (b"foobar", "string 'foobar'", 0x85944171f73967e8),
    ]
    for data, desc, expected in std_vectors:
        computed = fnv1a_64(data)
        assert computed == expected, f"Mismatch for {desc}: got {hex(computed)}, expected {hex(expected)}"
        print(f"  OK: {desc:16} {data!r:10} -> 0x{computed:016x} (matches published vector)")

    # 2. Canonical AST Test Cases Derived from Spec
    print("\n--- 2. Concrete Canonical Serializations and FNV-1a Hashes ---")
    test_cases = [
        ("Integer(0)", serialize_int(0)),
        ("Integer(42)", serialize_int(42)),
        ("Integer(-7)", serialize_int(-7)),
        ("Decimal(1.5)", serialize_dec(1.5)),
        ("Decimal(-0.25)", serialize_dec(-0.25)),
        ("Constant(pi)", serialize_const("pi")),
        ("Constant(e)", serialize_const("e")),
        ("Variable('x')", serialize_var("x")),
        ("BinOp('+', Variable('x'), Integer(1))", serialize_binop("+", serialize_var("x"), serialize_int(1))),
        ("Call('sin', Variable('x'))", serialize_call("sin", serialize_var("x"))),
        ("UnaryOp('-', Variable('x'))", serialize_unary("-", serialize_var("x"))),
    ]

    oracle_data = []
    for label, ser in test_cases:
        h = fnv1a_64(ser.encode("utf-8"))
        h_hex = f"0x{h:016x}"
        print(f"  {label:38} | Serialized: {ser:24} | Hash: {h_hex} ({h}u64)")
        oracle_data.append((label, ser, h, h_hex))

    # 3. Mutant Discrimination Demonstrations
    print("\n--- 3. Mutant Discrimination Proofs ---")
    
    # M35: 'I' -> 'J'
    ser_int_clean = serialize_int(42)  # "I42;"
    ser_int_mutant = f"J42;"
    h_m35_clean = fnv1a_64(ser_int_clean.encode("utf-8"))
    h_m35_mut = fnv1a_64(ser_int_mutant.encode("utf-8"))
    print(f"  M35 (tag I->J): Clean='{ser_int_clean}' (0x{h_m35_clean:016x}) vs Mutant='{ser_int_mutant}' (0x{h_m35_mut:016x}) -> DIFFER (KILLED)")
    assert h_m35_clean != h_m35_mut

    # M36: 'D' -> 'E'
    ser_dec_clean = serialize_dec(1.5)  # "D4609434218613702656;"
    ser_dec_mutant = f"E4609434218613702656;"
    h_m36_clean = fnv1a_64(ser_dec_clean.encode("utf-8"))
    h_m36_mut = fnv1a_64(ser_dec_mutant.encode("utf-8"))
    print(f"  M36 (tag D->E): Clean='{ser_dec_clean}' (0x{h_m36_clean:016x}) vs Mutant='{ser_dec_mutant}' (0x{h_m36_mut:016x}) -> DIFFER (KILLED)")
    assert h_m36_clean != h_m36_mut

    # M37: XOR -> Add in FNV-1a
    h_m37_clean = fnv1a_64(b"a")
    h_m37_mut = fnv1a_64_mutant_add(b"a")
    print(f"  M37 (XOR->Add): Clean=0x{h_m37_clean:016x} vs Mutant=0x{h_m37_mut:016x} -> DIFFER (KILLED)")
    assert h_m37_clean != h_m37_mut

    # M38: Initial basis constant mutated
    h_m38_clean = fnv1a_64(b"")
    h_m38_mut = fnv1a_64_mutant_basis(b"")
    print(f"  M38 (Basis+1): Clean=0x{h_m38_clean:016x} vs Mutant=0x{h_m38_mut:016x} -> DIFFER (KILLED)")
    assert h_m38_clean != h_m38_mut

    print("\nALL ORACLE CHECKS COMPLETED AND VALIDATED AGAINST EXTERNAL SPECIFICATIONS.")

if __name__ == "__main__":
    main()
