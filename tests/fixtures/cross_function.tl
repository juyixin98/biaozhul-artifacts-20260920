# Fixture: cross-function taint propagation (DANGEROUS)
# source() taint flows through two user functions via argument and return
# value before reaching sink().

func sanitize_like(x) {
    # NOTE: not actually called sanitize(); no taint is removed.
    y = x + 1;
    return y;
}

func forward(v) {
    z = sanitize_like(v);
    return z;
}

a = source();
b = forward(a);
sink(b);
