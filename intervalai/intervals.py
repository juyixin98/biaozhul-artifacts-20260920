"""Integer interval abstract domain over mathematical integers Z.

Intervals are represented as ``(lo, hi)`` pairs of ``int | None``; ``None``
stands for an infinite bound (``-oo`` / ``+oo``).  The distinguished
:data:`BOT` interval is the empty set (unreachable states).

All arithmetic is exact Python int arithmetic (arbitrary precision, no
machine-integer overflow), with floor division / remainder semantics
(``a // b``, ``a % b``, matching the reference interpreter).
"""

BOT = object()          # unique bottom sentinel
TOP = (None, None)      # (-oo, +oo)
ZERO = (0, 0)


def is_bot(v):
    return v is BOT


def make(lo, hi):
    """Build an interval, canonicalising empty intervals to BOT."""
    if lo is None or hi is None:
        if lo is not None and hi is not None:
            return BOT
        return (lo, hi)
    return (lo, hi) if lo <= hi else BOT


def const(x):
    return (x, x)


def finite(v):
    return not is_bot(v) and v[0] is not None and v[1] is not None


def lb(v):
    return v[0]


def hb(v):
    return v[1]


def contains(v, x):
    if is_bot(v):
        return False
    lo, hi = v
    return (lo is None or lo <= x) and (hi is None or x <= hi)


def subset(a, b):
    """a subseteq b (bottom is the least element)."""
    if is_bot(a):
        return True
    if is_bot(b):
        return False
    alo, ahi = a
    blo, bhi = b
    return (blo is None or (alo is not None and blo <= alo)) and \
           (bhi is None or (ahi is not None and ahi <= bhi))


def join(a, b):
    """Least upper bound (convex hull)."""
    if is_bot(a):
        return b
    if is_bot(b):
        return a
    alo, ahi = a
    blo, bhi = b
    lo = None if (alo is None or blo is None) else min(alo, blo)
    hi = None if (ahi is None or bhi is None) else max(ahi, bhi)
    return (lo, hi)


def meet(a, b):
    """Greatest lower bound (intersection of two convex intervals)."""
    if is_bot(a) or is_bot(b):
        return BOT
    alo, ahi = a
    blo, bhi = b
    lo = alo if blo is None else (blo if alo is None else max(alo, blo))
    hi = ahi if bhi is None else (bhi if ahi is None else min(ahi, bhi))
    return make(lo, hi)


def widen(a, b):
    """Standard interval widening: unstable bounds jump to infinity.

    ``a`` is the previous approximation, ``b`` the newly computed one.
    """
    if is_bot(a):
        return b
    if is_bot(b):
        return a
    alo, ahi = a
    blo, bhi = b
    # Standard interval widening. None at the lower-bound position means -oo
    # and at the upper-bound position means +oo (the two positions never mix
    # signs in a well-formed interval). A bound that is already infinite is
    # kept infinite; a finite new bound that crosses the previous finite bound
    # sends that bound to infinity.
    if alo is None or blo is None:
        lo = None
    elif blo < alo:
        lo = None
    else:
        lo = alo
    if ahi is None or bhi is None:
        hi = None
    elif bhi > ahi:
        hi = None
    else:
        hi = ahi
    return (lo, hi)


def narrow(a, b):
    """Standard interval narrowing: infinite bounds of ``a`` may be refined
    using the corresponding (stable) bound of ``b``; finite bounds are kept.
    """
    if is_bot(a) or is_bot(b):
        return a
    alo, ahi = a
    blo, bhi = b
    lo = blo if alo is None else alo
    hi = bhi if ahi is None else ahi
    return make(lo, hi)


# --------------------------------------------------------------- arithmetic

def neg(v):
    if is_bot(v):
        return BOT
    lo, hi = v
    return (None if hi is None else -hi,
            None if lo is None else -lo)


def add(a, b):
    if is_bot(a) or is_bot(b):
        return BOT
    alo, ahi = a
    blo, bhi = b
    lo = None if (alo is None or blo is None) else alo + blo
    hi = None if (ahi is None or bhi is None) else ahi + bhi
    return (lo, hi)


def sub(a, b):
    if is_bot(a) or is_bot(b):
        return BOT
    alo, ahi = a
    blo, bhi = b
    lo = None if (alo is None or bhi is None) else alo - bhi
    hi = None if (ahi is None or blo is None) else ahi - blo
    return (lo, hi)


def _sign_parts(v):
    """Return (nonnegative part or None, nonpositive part or None,
    contains zero)."""
    lo, hi = v
    pos = make(max(lo, 0) if lo is not None else 0, hi)
    neg = make(lo, min(hi, 0) if hi is not None else 0)
    zero = contains(v, 0)
    return (None if is_bot(pos) else pos,
            None if is_bot(neg) else neg,
            zero)


def mul(a, b):
    """Sound multiplication over intervals with infinite endpoints.

    Split each operand into a nonnegative part (magnitude interval starting at
    0) and a nonpositive part, then combine the four sign pairs.  Each
    magnitude product is computed with extended arithmetic where an infinite
    magnitude times a strictly-positive finite/infinite magnitude is infinite
    (and infinity times the single point 0 is 0).
    """
    if is_bot(a) or is_bot(b):
        return BOT
    a_pos, a_neg, a_zero = _sign_parts(a)
    b_pos, b_neg, b_zero = _sign_parts(b)
    acc = BOT
    for pa, sa in ((a_pos, +1), (a_neg, -1)):
        if pa is None:
            continue
        for pb, sb in ((b_pos, +1), (b_neg, -1)):
            if pb is None:
                continue
            mag = _mul_nonneg_magnitudes(_mag(pa, sa), _mag(pb, sb))
            sign = sa * sb
            acc = join(acc, _apply_mag_sign(mag, sign))
    if a_zero or b_zero:
        acc = join(acc, ZERO)
    return acc


def _mag(part, sign):
    """Magnitude interval [m0,m1] (m >= 0 endpoints, possibly infinite) for a
    nonnegative (sign=+1) or nonpositive (sign=-1) part touching zero."""
    lo, hi = part
    if sign > 0:
        return (lo, hi)                       # [>=0]
    # nonpositive part: hi <= 0 <= ... magnitudes are [-hi, -lo]
    return (-hi if hi is not None else None,
            -lo if lo is not None else None)


def _mul_nonneg_magnitudes(u, v):
    """Product of two magnitude intervals with nonnegative (possibly +oo)
    endpoints.  Returns (mlo, mhi)."""
    u0, u1 = u
    v0, v1 = v
    mlo = u0 * v0                              # both finite lower magnitudes
    if u1 is None or v1 is None:
        # An infinite magnitude meets a magnitude that can be (strictly)
        # positive -> unbounded; +oo * the single point 0 is 0.
        u_present_pos = u1 != 0
        v_present_pos = v1 != 0
        if (u1 is None and v1 is not None and v1 == 0) or \
           (v1 is None and u1 is not None and u1 == 0):
            mhi = 0
        elif (u1 is None and not v_present_pos) or \
             (v1 is None and not u_present_pos):
            mhi = 0
        else:
            mhi = None
    else:
        mhi = u1 * v1
    return (mlo, mhi)


def _apply_mag_sign(mag, sign):
    m0, m1 = mag
    if sign > 0:
        return (m0, m1)
    return (None if m1 is None else -m1,
            None if m0 is None else -m0)


def div(a, b):
    """Sound floor-division interval.

    The divisor is split into its positive and negative pieces (zero is never
    a valid divisor).  Floor division is monotone nondecreasing in the
    numerator for either sign of divisor, so the extrema over a sign-definite
    divisor piece occur at the corner pairs; infinite numerator ends stay
    infinite.  Returns ``(quotient, may_div_zero)``.
    """
    if is_bot(a) or is_bot(b):
        return BOT, False
    may_zero = contains(b, 0)
    blo, bhi = b
    pos = make(1 if blo is None or blo < 1 else blo, bhi)
    neg = make(blo, -1 if bhi is None or bhi > -1 else bhi)

    acc = BOT
    # positive piece: plo finite (>=1), phi may be +oo
    # negative piece: phi finite (<=-1), plo may be -oo
    if not is_bot(pos):
        acc = join(acc, _div_piece(a, pos, divisor_sign=+1))
    if not is_bot(neg):
        acc = join(acc, _div_piece(a, neg, divisor_sign=-1))
    return acc, may_zero


def _div_piece(a, piece, divisor_sign):
    """Extrema of floor(x/y) for x in interval a and y a sign-definite piece
    (possibly with one infinite end).  ``None`` ends mean infinity."""
    alo, ahi = a
    plo, phi = piece
    finite_divisor = plo is not None and phi is not None

    # Candidate quotients at finite numerator ends against finite divisor ends.
    corners = []
    for x in (alo, ahi):
        if x is None:
            continue
        for y in (plo, phi):
            if y is not None:
                corners.append(_fdiv(x, y))

    if not finite_divisor:
        # |y| can grow without bound.
        if alo is None or ahi is None:
            # An unbounded numerator over an unbounded divisor is unconstrained.
            return TOP
        lo = min(corners + [0])
        hi = max(corners + [0])
        return (lo, hi)

    lo = min(corners) if corners else 0
    hi = max(corners) if corners else 0
    # Divisor bounded away from zero; infinite numerator ends stay infinite,
    # with sign flipped for a negative divisor.
    if alo is None:
        if divisor_sign > 0:
            lo = None
        else:
            hi = None
    if ahi is None:
        if divisor_sign > 0:
            hi = None
        else:
            lo = None
    return (lo, hi)


def _fdiv(x, y):
    # Python // is floor division for arbitrary precision ints.
    return x // y


def mod(a, b):
    """Sound floor-remainder: ``a % b == a - (a//b)*b``; the result has the
    sign of the divisor (``0 <= r < b`` for positive b, ``b < r <= 0`` for
    negative b).

    Returns ``(remainder_interval, may_div_zero)``.
    """
    if is_bot(a) or is_bot(b):
        return BOT, False
    may_zero = contains(b, 0)
    blo, bhi = b
    pos = make(1 if blo is None or blo < 1 else blo, bhi)
    neg = make(blo, -1 if bhi is None or bhi > -1 else bhi)

    acc = BOT
    for p in (pos, neg):
        if is_bot(p):
            continue
        plo, phi = p
        if finite(a) and plo is not None and phi is not None:
            xs = (a[0], a[1])
            ys = (plo, phi)
            rs = [x % y for x in xs for y in ys]
            acc = join(acc, (min(rs), max(rs)))
            # Interior numerators can realise the full sign-of-divisor
            # remainder range; add the generic bound for safety.
            m = max(abs(plo), abs(phi))
            acc = join(acc, (-m + 1, m - 1))
        elif plo is not None and phi is not None:
            # Infinite numerator; remainder is still bounded by |divisor|.
            m = max(abs(plo), abs(phi))
            acc = join(acc, (-m + 1, m - 1))
        else:
            # Divisor magnitude unbounded: the remainder is unconstrained.
            acc = join(acc, TOP)
    return acc, may_zero


def to_str(v):
    if is_bot(v):
        return "_|_"
    lo, hi = v
    return f"[{_elo(lo)}, {_ehi(hi)}]"


def _elo(x):
    return "-oo" if x is None else str(x)


def _ehi(x):
    return "+oo" if x is None else str(x)
