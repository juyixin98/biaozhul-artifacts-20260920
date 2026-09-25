package invidx.engine;

/** Error thrown by crash-injection hooks to simulate a hard process kill. */
public class SimulatedCrash extends Error {

    public final String point;

    public SimulatedCrash(String point) {
        super("simulated crash at: " + point);
        this.point = point;
    }
}
