package com.example.cdc;

/** 测试总入口：注册全部用例并运行。 */
public final class RunTests {

    public static void main(String[] args) {
        System.out.println("== 引擎语义测试 ==");
        EngineTest.register();
        System.out.println("== HTTP 端到端测试 ==");
        HttpTest.register();
        Test.run();
    }

    private RunTests() {
    }
}
