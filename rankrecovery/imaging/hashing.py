"""Content and perceptual hashing for duplicate detection."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

import imagehash
from PIL import Image


class DuplicateClass(StrEnum):
    EXACT = "exact"
    DUPLICATE = "duplicate"
    NEAR_DUPLICATE = "near_duplicate"
    DISTINCT = "distinct"


@dataclass(frozen=True, slots=True)
class DuplicateThresholds:
    duplicate_distance: int = 6
    near_duplicate_distance: int = 12


DEFAULT_THRESHOLDS = DuplicateThresholds()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def perceptual_hash(img: Image.Image, *, hash_size: int = 16) -> str:
    """Perceptual hash (pHash) of an image as a hex string."""
    return str(imagehash.phash(img.convert("L"), hash_size=hash_size))


def hamming_distance(hash_a: str, hash_b: str) -> int:
    if not hash_a or not hash_b:
        return 1 << 30
    try:
        return int(imagehash.hex_to_hash(hash_a) - imagehash.hex_to_hash(hash_b))
    except Exception:
        # Fall back to a bitwise compare of the hex strings when sizes differ.
        a, b = int(hash_a, 16), int(hash_b, 16)
        return bin(a ^ b).count("1")


def classify_duplicate(
    *,
    sha_a: str,
    sha_b: str,
    phash_a: str | None,
    phash_b: str | None,
    thresholds: DuplicateThresholds | None = None,
) -> tuple[DuplicateClass, int]:
    """Classify two images. Returns ``(class, perceptual_distance)``."""
    thresholds = thresholds or DEFAULT_THRESHOLDS
    if sha_a and sha_a == sha_b:
        return DuplicateClass.EXACT, 0
    if not phash_a or not phash_b:
        return DuplicateClass.DISTINCT, 1 << 30
    distance = hamming_distance(phash_a, phash_b)
    if distance <= thresholds.duplicate_distance:
        return DuplicateClass.DUPLICATE, distance
    if distance <= thresholds.near_duplicate_distance:
        return DuplicateClass.NEAR_DUPLICATE, distance
    return DuplicateClass.DISTINCT, distance
