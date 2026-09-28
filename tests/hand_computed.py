"""Hand-computed expected values for the fixture corpora.

Every number below was derived by hand from the fixture tables (see
docs/semantics.md for the formulas) and is expressed as an exact Fraction.
These constants are the primary oracle; tests/reference_impl.py is the
secondary, independent cross-check.
"""
from __future__ import annotations

from fractions import Fraction as F

# ---- basic.json (N = 5) ----------------------------------------------------
# counts: bread 4, milk 4, diapers 4, beer 3, cola 2, eggs 1
BASIC_N = 5

BASIC_FREQUENT_0_6 = {
    # min_support 0.6 -> count >= 3
    frozenset(["bread"]): 4,
    frozenset(["milk"]): 4,
    frozenset(["diapers"]): 4,
    frozenset(["beer"]): 3,
    frozenset(["bread", "milk"]): 3,
    frozenset(["bread", "diapers"]): 3,
    frozenset(["milk", "diapers"]): 3,
    frozenset(["diapers", "beer"]): 3,
}

# {beer} -> {diapers}: conf 3/3 = 1, lift 1/(4/5) = 5/4, leverage 3/5 - 12/25 = 3/25
BEER_TO_DIAPERS = {
    "support": F(3, 5),
    "confidence": F(1, 1),
    "lift": F(5, 4),
    "leverage": F(3, 25),
}

# {milk} -> {bread}: conf 3/4, lift (3/4)/(4/5) = 15/16, leverage 3/5 - 16/25 = -1/25
MILK_TO_BREAD = {
    "support": F(3, 5),
    "confidence": F(3, 4),
    "lift": F(15, 16),
    "leverage": F(-1, 25),
}

# min_support 0.4 -> count >= 2; {milk,diapers,beer} count 2 (T3, T4)
# {milk,diapers} -> {beer}: conf 2/3, lift (2/3)/(3/5) = 10/9, leverage 2/5 - 9/25 = 1/25
MILK_DIAPERS_TO_BEER = {
    "support": F(2, 5),
    "confidence": F(2, 3),
    "lift": F(10, 9),
    "leverage": F(1, 25),
}

# At min_support 0.6 and min_confidence 0.8 exactly one rule survives:
# {beer} -> {diapers} (confidence 1.0). Used to check pruning loses nothing.
BASIC_RULES_MINCONF_0_8 = [(frozenset(["beer"]), frozenset(["diapers"]))]

# ---- ubiquitous.json (N = 6) ------------------------------------------------
# base 6/6, apple 3/6; {apple} -> {base}: conf 1, lift 1, leverage 0
APPLE_TO_BASE = {
    "support": F(1, 2),
    "confidence": F(1, 1),
    "lift": F(1, 1),
    "leverage": F(0, 1),
}

# ---- exclusive.json (N = 4) -------------------------------------------------
# alpha 2/4, beta 2/4, joint 0 -> conf 0, lift 0, leverage -1/4
ALPHA_TO_BETA = {
    "support": F(0, 1),
    "confidence": F(0, 1),
    "lift": F(0, 1),
    "leverage": F(-1, 4),
}

# ---- rare_combo.json (N = 40) ----------------------------------------------
# rareA 2/40, rareB 2/40, joint 2/40 -> conf 1, lift 20, leverage 1/20 - 1/400 = 19/400
RAREA_TO_RAREB = {
    "support": F(1, 20),
    "confidence": F(1, 1),
    "lift": F(20, 1),
    "leverage": F(19, 400),
}

# ---- duplicates.json (N = 3) ------------------------------------------------
# raw: D1 x,x,y,y,y ; D2 x,z ; D3 y,z,z  -> dedup {x,y},{x,z},{y,z}
DUPLICATES_N = 3
DUPLICATES_REMOVED = 4  # 3 in D1 + 1 in D3
DUPLICATES_SUPPORT = {
    frozenset(["x"]): 2,
    frozenset(["y"]): 2,
    frozenset(["z"]): 2,
    frozenset(["x", "y"]): 1,
    frozenset(["x", "z"]): 1,
    frozenset(["y", "z"]): 1,
}
