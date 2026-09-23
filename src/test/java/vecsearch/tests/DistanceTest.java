package vecsearch.tests;

import vecsearch.core.Distances;
import vecsearch.core.Metric;
import vecsearch.testutil.Assert;
import vecsearch.testutil.TestRunner;

/** Distances 的数值正确性与零向量行为。 */
public final class DistanceTest {

    public static void register(TestRunner r) {
        r.test("L2 basic: 3-4-5 triangle", () -> {
            float d = Distances.l2(new float[]{0, 0}, new float[]{3, 4});
            Assert.approx(d, 5.0, 1e-6);
        });

        r.test("L2 identical vectors is 0", () -> {
            float d = Distances.l2(new float[]{1, -2, 3}, new float[]{1, -2, 3});
            Assert.approx(d, 0.0, 1e-9);
        });

        r.test("L2 is symmetric", () -> {
            float[] a = {1.5f, -2.0f, 0.3f};
            float[] b = {-0.7f, 2.2f, 1.1f};
            Assert.approx(Distances.l2(a, b), Distances.l2(b, a), 1e-6);
        });

        r.test("cosine orthogonal is 1", () -> {
            float d = Distances.cosine(new float[]{1, 0}, new float[]{0, 1});
            Assert.approx(d, 1.0, 1e-6);
        });

        r.test("cosine same direction is 0, opposite is 2", () -> {
            float[] a = {1, 1};
            float[] b = {3, 3};
            float[] c = {-2, -2};
            Assert.approx(Distances.cosine(a, b), 0.0, 1e-6);
            Assert.approx(Distances.cosine(a, c), 2.0, 1e-6);
        });

        r.test("cosine rejects zero vector (either side)", () -> {
            Assert.throws_(IllegalArgumentException.class,
                    () -> Distances.cosine(new float[]{0, 0}, new float[]{1, 1}),
                    "zero on left");
            Assert.throws_(IllegalArgumentException.class,
                    () -> Distances.cosine(new float[]{1, 1}, new float[]{0, 0}),
                    "zero on right");
        });

        r.test("L2 accepts zero vector", () -> {
            Assert.approx(Distances.l2(new float[]{0, 0}, new float[]{3, 4}), 5.0, 1e-6);
        });

        r.test("normalize produces unit length and keeps direction", () -> {
            float[] u = Distances.normalize(new float[]{3, 4});
            Assert.approx(Distances.norm(u), 1.0, 1e-6);
            Assert.approx(u[0], 0.6, 1e-6);
            Assert.approx(u[1], 0.8, 1e-6);
        });

        r.test("normalize rejects zero", () -> {
            Assert.throws_(IllegalArgumentException.class,
                    () -> Distances.normalize(new float[]{0, 0, 0}), "zero normalize");
        });

        r.test("dimension mismatch throws", () -> {
            Assert.throws_(IllegalArgumentException.class,
                    () -> Distances.l2(new float[]{1, 2, 3}, new float[]{1, 2}), "l2 dim");
            Assert.throws_(IllegalArgumentException.class,
                    () -> Distances.cosine(new float[]{1, 2, 3}, new float[]{1, 2}), "cos dim");
        });

        r.test("Metric parsing aliases", () -> {
            Assert.eq(Metric.fromString("l2"), Metric.L2);
            Assert.eq(Metric.fromString("EUCLIDEAN"), Metric.L2);
            Assert.eq(Metric.fromString("cos"), Metric.COSINE);
            Assert.eq(Metric.fromString("COSINE"), Metric.COSINE);
            Assert.eq(Metric.fromString("manhattan"), null);
        });
    }

    private DistanceTest() {
    }
}
