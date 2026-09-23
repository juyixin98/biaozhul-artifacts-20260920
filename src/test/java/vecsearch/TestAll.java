package vecsearch;

import vecsearch.tests.DistanceTest;
import vecsearch.tests.HttpTest;
import vecsearch.tests.IvfTest;
import vecsearch.tests.JsonTest;
import vecsearch.tests.StoreTest;
import vecsearch.testutil.TestRunner;

import java.util.List;

/**
 * 全部自动化测试的入口（零依赖，不使用 JUnit）：
 * <pre>java -cp build/classes:build/test-classes vecsearch.TestAll</pre>
 * 全部通过退出码 0，否则 1。
 */
public final class TestAll {

    public static void main(String[] args) {
        TestRunner distance = new TestRunner("Distances");
        DistanceTest.register(distance);

        TestRunner store = new TestRunner("VectorStore");
        StoreTest.register(store);

        TestRunner ivf = new TestRunner("IvfIndex");
        IvfTest.register(ivf);

        TestRunner json = new TestRunner("Json");
        JsonTest.register(json);

        TestRunner http = new TestRunner("HttpEndToEnd");
        HttpTest.register(http);

        boolean ok = true;
        for (TestRunner r : List.of(distance, store, ivf, json, http)) {
            ok &= r.run();
        }
        if (ok) {
            System.out.println("ALL TESTS PASSED");
            System.exit(0);
        } else {
            System.out.println("SOME TESTS FAILED");
            System.exit(1);
        }
    }
}
