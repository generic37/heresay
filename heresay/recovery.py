# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One-time recovery codes — self-serve password recovery with zero PII.

A code is plain and typeable: ``word-word-word-NNNN`` (three words
plus four digits). Every choice uses :mod:`secrets`. The server stores only
argon2id hashes of codes (see ``AccountStore``); the plaintext is shown once at
generation and never again — using one burns it, regenerating voids the set.

Entropy: 3 * log2(len(WORDS)) + log2(10000). With this word list that is well
over the 40-bit target (asserted at import).
"""

from __future__ import annotations

import math
import re
import secrets

CODES_PER_SET = 5
WORDS_PER_CODE = 3
DIGITS = 4

# A few hundred short, typeable words. Entropy comes from `secrets`, not from the
# words; the list only has to be large and unmistakable read aloud. ⛔ It is
# deliberately GENERIC: this is ENGINE code, and a word list carrying one
# instance's flavour would put that instance's world into every house that ran
# the engine. (Kept lowercase; codes are matched case-insensitively.)
WORDS = [
    # Everyday things — short, typeable, unambiguous.
    "kitchen", "pantry", "cellar", "attic", "hallway", "landing", "porch",
    "parlour", "study", "library", "mantel", "stove", "oven", "skillet",
    "napkin", "platter", "tumbler", "pitcher", "crock", "churn", "barrel",
    "taper", "beacon", "signal", "ferry", "harbour", "wharf", "quay", "jetty",
    "pier", "lighthouse", "compass", "rudder", "tiller", "keel", "prow",
    "ledger", "tally", "receipt", "invoice", "stamp", "ribbon", "twine",
    "cord", "clasp", "buckle", "button", "zipper", "bobbin", "spool",
    "chisel", "plane", "file", "rasp", "awl", "anvil", "bellows", "forge",
    "kiln", "loom", "shuttle", "spindle", "distaff", "fleece", "canvas",
    "satin", "paddock", "stable", "byre", "barn", "silo", "trough", "manger",
    "paddle", "harrow", "plough", "shepherd", "cooper", "tanner", "potter",
    "mason", "glazier", "joiner", "farrier", "miller", "almanac", "calendar",
    "hourglass", "sundial", "pendulum", "barometer", "telescope", "lens",
    "prism", "marble", "domino", "counter", "token", "dice", "draughts",
    "checker", "abacus", "lozenge", "biscuit", "crumpet", "scone", "wafer",
    "syrup", "honey", "butter", "cream", "custard", "satchel", "knapsack",
    "hamper", "trolley", "barrow", "ladder", "trestle", "carpet", "rug",
    "blanket", "quilt", "pillow", "bolster", "mattress", "curtain", "shutter",
    "awning", "chimney", "gable", "eaves", "rafter", "beam", "joist", "plank",
    "shingle", "slate", "thatch", "meadowland", "pasture", "allotment",
    "nursery", "greenhouse", "conservatory", "verandah", "balcony", "terrace",
    "courtyard", "kettleful", "teaspoon",
    # Nature
    "river", "stream", "brook", "pond", "lake", "ocean", "sea", "wave", "tide",
    "beach", "sand", "shell", "coral", "reef", "island", "cove", "cliff",
    "mountain", "hill", "valley", "meadow", "field", "forest", "wood", "grove",
    "tree", "oak", "elm", "pine", "birch", "willow", "maple", "aspen", "cedar",
    "leaf", "branch", "root", "bark", "moss", "fern", "vine", "thorn", "bud",
    "bloom", "petal", "pollen", "seed", "acorn", "berry", "apple", "plum",
    "pear", "peach", "cherry", "lemon", "grape", "melon", "fig", "date",
    "wind", "breeze", "gale", "storm", "cloud", "rain", "snow", "frost", "ice",
    "hail", "mist", "fog", "dew", "sun", "moon", "star", "comet", "sky",
    "dawn", "dusk", "noon", "night", "shadow", "light", "flame", "ember",
    "spark", "smoke", "ash", "stone", "rock", "pebble", "boulder", "gravel",
    "clay", "mud", "dust", "sand", "iron", "copper", "silver", "gold", "brass",
    "amber", "pearl", "ruby", "opal", "jade", "onyx", "quartz", "crystal",
    # Animals
    "fox", "wolf", "bear", "deer", "elk", "moose", "hare", "otter", "badger",
    "beaver", "hedgehog", "mole", "shrew", "stoat", "weasel", "ferret", "lynx",
    "hawk", "owl", "eagle", "falcon", "sparrow", "robin", "wren", "finch",
    "lark", "swift", "swan", "goose", "duck", "heron", "stork", "crane",
    "magpie", "jay", "dove", "pigeon", "gull", "tern", "puffin", "kestrel",
    "frog", "toad", "newt", "snake", "adder", "viper", "gecko", "skink",
    "trout", "salmon", "carp", "pike", "perch", "bass", "eel", "ray", "shark",
    "whale", "dolphin", "seal", "walrus", "crab", "shrimp", "prawn", "squid",
    "bee", "wasp", "ant", "moth", "fly", "gnat", "beetle", "cricket", "mantis",
    "spider", "snail", "slug", "worm", "grub", "larva", "midge", "aphid",
    # Objects / everyday
    "lamp", "candle", "lantern", "torch", "wick", "match", "coal", "hearth",
    "chair", "stool", "bench", "table", "shelf", "cupboard", "drawer", "chest",
    "box", "crate", "basket", "bucket", "pail", "jar", "jug", "flask", "cup",
    "mug", "plate", "bowl", "dish", "tray", "fork", "knife", "ladle", "whisk",
    "book", "page", "cover", "spine", "quill", "pen", "ink", "paper", "scroll",
    "map", "chart", "key", "lock", "latch", "hinge", "bolt", "chain", "rope",
    "thread", "yarn", "cloth", "linen", "silk", "velvet", "cotton", "wool",
    "coat", "cloak", "hood", "hat", "cap", "boot", "shoe", "sock", "glove",
    "clock", "bell", "chime", "gong", "drum", "flute", "harp", "fiddle",
    "coin", "purse", "pouch", "sack", "bag", "parcel", "bundle", "packet",
    "wheel", "cart", "wagon", "sled", "raft", "boat", "sail", "anchor", "mast",
    "bridge", "gate", "door", "window", "arch", "tower", "spire", "vault",
    # Colors / qualities
    "red", "green", "blue", "yellow", "purple", "orange", "pink", "brown",
    "black", "white", "grey", "scarlet", "crimson", "azure", "indigo", "violet",
    "teal", "olive", "maroon", "ivory", "ebony", "amber", "russet", "umber",
    "bright", "dark", "pale", "vivid", "dim", "faint", "bold", "soft", "sharp",
    "swift", "slow", "quiet", "loud", "calm", "wild", "gentle", "fierce",
    "warm", "cool", "cold", "hot", "damp", "dry", "smooth", "rough", "keen",
    "merry", "solemn", "curious", "clever", "silly", "brave", "shy", "proud",
    "quaint", "queer", "odd", "plain", "fancy", "humble", "grand", "tidy",
    # Verbs-as-nouns / misc
    "leap", "bound", "dash", "creep", "wander", "roam", "drift", "float",
    "tumble", "spin", "twirl", "sway", "shiver", "quiver", "flutter", "hover",
    "ponder", "muse", "dream", "wonder", "puzzle", "guess", "reckon", "figure",
    "whisper", "murmur", "hum", "sing", "chant", "recite", "mutter", "babble",
]

# Deduplicate while preserving order (the hand-written list has intentional
# overlaps between themes).
WORDS = list(dict.fromkeys(WORDS))


def entropy_bits() -> float:
    return WORDS_PER_CODE * math.log2(len(WORDS)) + math.log2(10 ** DIGITS)


assert len(WORDS) >= 478, f"word list too small ({len(WORDS)})"
assert entropy_bits() >= 40.0, f"only {entropy_bits():.1f} bits per code"


def generate_code() -> str:
    words = [secrets.choice(WORDS) for _ in range(WORDS_PER_CODE)]
    number = secrets.randbelow(10 ** DIGITS)
    return "-".join(words) + f"-{number:0{DIGITS}d}"


def generate_set(n: int = CODES_PER_SET) -> list[str]:
    return [generate_code() for _ in range(n)]


def normalize(code: str) -> str:
    """Canonical form for hashing and matching: lowercase, and any run of
    spaces / hyphens / underscores collapsed to a single hyphen. So
    "Copper Lantern Tide 4711" and "COPPER-LANTERN-TIDE-4711" both normalize the same."""
    parts = [p for p in re.split(r"[\s\-_]+", code.strip().lower()) if p]
    return "-".join(parts)
