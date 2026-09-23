package ij;

/** 一次左-右配对的结果。 */
final class Pair {
    final Event left;
    final Event right;

    Pair(Event left, Event right) {
        this.left = left;
        this.right = right;
    }
}
