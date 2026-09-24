# Fixture: loop-carried taint via fixpoint (DANGEROUS)
# On iteration 1 p is clean; on later iterations it carries source taint.
# Only a fixpoint (not a single-pass AST walk) finds this.

p = 0;
i = 0;
while (i < 10) {
    if (i == 3) {
        p = source();
    }
    i = i + 1;
}
sink(p);
