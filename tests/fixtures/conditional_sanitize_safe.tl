# Fixture: unconditional sanitization (SAFE)
# sanitize() dominates the sink on every path; no finding expected.

x = source();
x = sanitize(x);
if (true) {
    x = sanitize(x);
} else {
    x = sanitize(x);
}
sink(x);
