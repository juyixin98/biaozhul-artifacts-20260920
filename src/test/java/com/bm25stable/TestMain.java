package com.bm25stable;

/** 测试入口：运行全部测试，全部通过时退出码 0，否则 1。 */
public final class TestMain {

    private TestMain() {
    }

    public static void main(String[] args) throws Exception {
        TestFramework.run(TokenizerTest.class);
        TestFramework.run(BM25Test.class);
        TestFramework.run(PaginationTest.class);
        TestFramework.run(SnapshotTest.class);

        HttpApiTest.setUp();
        try {
            TestFramework.run(HttpApiTest.class);
        } finally {
            HttpApiTest.tearDown();
        }

        System.exit(TestFramework.summary() ? 0 : 1);
    }
}
