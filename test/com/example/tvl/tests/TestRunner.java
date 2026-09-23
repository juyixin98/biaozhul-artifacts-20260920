package com.example.tvl.tests;

import java.util.ArrayList;
import java.util.List;

/**
 * 全部自动化测试入口：
 *   java -cp out/classes:out/test-classes com.example.tvl.tests.TestRunner
 *
 * 运行三值逻辑穷举、类型检查、空/跨批次、解析器与 HTTP 端到端五个套件。
 * 任一失败则以非零状态码退出。
 */
public final class TestRunner {

    public static void main(String[] args) {
        List<MiniTest.Suite> suites = new ArrayList<>();
        suites.add(ThreeValueLogicTest.suite());
        suites.add(TypeSystemTest.suite());
        suites.add(BatchExecutionTest.suite());
        suites.add(ParserTest.suite());
        suites.add(HttpEndToEndTest.suite());

        boolean allPassed = true;
        int totalPass = 0;
        int totalFail = 0;
        for (MiniTest.Suite s : suites) {
            boolean ok = s.run();
            allPassed &= ok;
            System.out.println();
        }
        // 套件自身统计通过 run 输出；此处仅汇总退出码
        System.out.println(allPassed
                ? "全部测试通过（核心查询逻辑无任何外部 SQL 引擎参与）"
                : "存在失败用例");
        if (!allPassed) {
            System.exit(1);
        }
    }

    private TestRunner() {}
}
