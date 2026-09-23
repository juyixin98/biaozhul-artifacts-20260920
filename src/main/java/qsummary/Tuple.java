package qsummary;

/**
 * One stored tuple of a Greenwald-Khanna summary.
 *
 * A tuple (v, g, delta) represents the value {@code v} together with:
 *   g     = rmin(v) - rmin(previous tuple), a positive integer count,
 *   delta = rmax(v) - rmin(v), the uncertainty band width.
 *
 * Rank convention: ranks are 1-based counts of values <= v, as in the
 * Greenwald-Khanna paper. The first tuple has rmin = 1, and the invariant
 * after every compress is   g + delta <= floor(2 * epsilon * n).
 */
final class Tuple {
    final double value;
    int g;
    int delta;

    Tuple(double value, int g, int delta) {
        this.value = value;
        this.g = g;
        this.delta = delta;
    }
}
