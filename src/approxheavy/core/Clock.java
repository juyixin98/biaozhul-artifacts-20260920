package approxheavy.core;

/** Injectable clock, so deterministic tests never touch the wall clock. */
public interface Clock {
    long millis();
}
