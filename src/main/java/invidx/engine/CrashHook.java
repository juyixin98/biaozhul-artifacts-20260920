package invidx.engine;

/**
 * Hook invoked at named durability points. A test implementation throws
 * {@link SimulatedCrash} to emulate a hard kill immediately after the
 * preceding fsync/rename.
 */
@FunctionalInterface
public interface CrashHook {
    CrashHook NOOP = point -> {
    };

    void onPoint(String point);
}
