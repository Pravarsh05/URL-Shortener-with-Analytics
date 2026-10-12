import pytest
from app.key_generator import (
    encode_base62,
    decode_base62,
    scramble_id,
    unscramble_id,
    generate_code_from_id,
    get_id_from_code,
    MAX_DOMAIN,
)

def test_base62_round_trip():
    test_values = [0, 1, 61, 62, 1000, 1234567, 987654321, MAX_DOMAIN - 1]
    for val in test_values:
        encoded = encode_base62(val, min_length=7)
        assert len(encoded) >= 7
        decoded = decode_base62(encoded)
        assert decoded == val

def test_scramble_unscramble_bijection():
    test_ids = [1, 2, 3, 42, 100, 9999, 1000000, 50000000]
    for seq_id in test_ids:
        scrambled = scramble_id(seq_id)
        assert scrambled != seq_id, "Scrambled ID should look completely different from sequential ID"
        recovered = unscramble_id(scrambled)
        assert recovered == seq_id, f"Failed to recover {seq_id} after scramble"

def test_code_generation_length_and_characters():
    allowed_chars = set("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
    for i in range(1, 50):
        code = generate_code_from_id(i)
        assert len(code) == 7, f"Expected 7 characters, got {len(code)}"
        assert all(c in allowed_chars for c in code), f"Invalid character in code {code}"

def test_non_sequential_codes():
    # Codes for sequential numbers 1, 2, 3 should not have obvious sequential patterns
    code1 = generate_code_from_id(1)
    code2 = generate_code_from_id(2)
    code3 = generate_code_from_id(3)

    assert code1 != code2 != code3
    # Verify high hamming distance (not just incrementing last char)
    diffs = sum(1 for a, b in zip(code1, code2) if a != b)
    assert diffs >= 4, f"Sequential codes {code1} and {code2} are too similar ({diffs} differences)"

def test_full_round_trip():
    for test_id in [1, 100, 54321, 9876543]:
        code = generate_code_from_id(test_id)
        recovered_id = get_id_from_code(code)
        assert recovered_id == test_id
