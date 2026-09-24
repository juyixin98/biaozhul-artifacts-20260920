# Fixture: conditional sanitization.
#
# Only the `if` arm sanitizes. The analyzer is path-insensitive (may
# analysis), so after the join the variable may still be tainted on the
# un-sanitized arm and this MUST be reported -- even though a human
# reading the constant condition might say the taken arm is the clean one.
# This fixture documents the false-positive boundary.

x = source();
if (true) {
    x = sanitize(x);
}
sink(x);
