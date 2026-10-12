"""
Key generator implementing Counter + Reversible Feistel Cipher + Base62 encoding.
Converts an auto-incrementing integer ID into a non-sequential, unguessable 7-character code.
Guarantees zero collisions within a space of 62^7 (~3.52 trillion) URLs using cycle-walking.
"""

BASE62_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
BASE = len(BASE62_ALPHABET)
MAX_DOMAIN = 62**7  # 3,521,614,606,208

FEISTEL_KEYS = [0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xCA62C1D6]

def _feistel_round(val: int, key: int) -> int:
    """Internal mixing round for 21-bit half."""
    return (val * 1103515245 + key) & 0x1FFFFF

def _feistel_42_forward(n: int) -> int:
    """4-round Feistel forward pass on 42-bit integer."""
    left = (n >> 21) & 0x1FFFFF
    right = n & 0x1FFFFF
    for k in FEISTEL_KEYS:
        f = _feistel_round(right, k)
        left, right = right, left ^ f
    return (left << 21) | right

def _feistel_42_inverse(n: int) -> int:
    """4-round Feistel inverse pass on 42-bit integer."""
    left = (n >> 21) & 0x1FFFFF
    right = n & 0x1FFFFF
    for k in reversed(FEISTEL_KEYS):
        f = _feistel_round(left, k)
        right, left = left, right ^ f
    return (left << 21) | right

def scramble_id(n: int) -> int:
    """
    Reversibly scrambles sequential integers into pseudo-random numbers
    in the range [0, MAX_DOMAIN).
    Uses a 4-round Feistel network with cycle-walking format-preserving encryption (FPE).
    """
    cur = n % MAX_DOMAIN
    while True:
        cur = _feistel_42_forward(cur)
        if cur < MAX_DOMAIN:
            return cur

def unscramble_id(scrambled: int) -> int:
    """
    Reverses the scramble operation back to the original counter ID.
    """
    cur = scrambled % MAX_DOMAIN
    while True:
        cur = _feistel_42_inverse(cur)
        if cur < MAX_DOMAIN:
            return cur

def encode_base62(num: int, min_length: int = 7) -> str:
    """Encodes an integer into a Base62 string."""
    if num == 0:
        return BASE62_ALPHABET[0].rjust(min_length, BASE62_ALPHABET[0])

    digits = []
    while num > 0:
        remainder = num % BASE
        digits.append(BASE62_ALPHABET[remainder])
        num = num // BASE

    digits.reverse()
    result = "".join(digits)
    return result.rjust(min_length, BASE62_ALPHABET[0])

def decode_base62(s: str) -> int:
    """Decodes a Base62 string back to an integer."""
    num = 0
    for char in s:
        idx = BASE62_ALPHABET.find(char)
        if idx == -1:
            raise ValueError(f"Invalid character '{char}' in Base62 string")
        num = num * BASE + idx
    return num

def generate_code_from_id(link_id: int) -> str:
    """Generates a non-sequential 7-character short code from a database sequence ID."""
    scrambled = scramble_id(link_id)
    return encode_base62(scrambled, min_length=7)

def get_id_from_code(code: str) -> int:
    """Extracts original sequence ID from a generated short code."""
    scrambled = decode_base62(code)
    return unscramble_id(scrambled)
