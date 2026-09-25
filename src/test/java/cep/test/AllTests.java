package cep.test;

import cep.json.JsonParserTest;
import cep.pattern.BruteForceEquivalenceTest;
import cep.pattern.EnginePolicyTest;
import cep.pattern.HandComputedScenarioTest;
import cep.pattern.LateDataTest;
import cep.pattern.TotalOrderAndTimeoutTest;
import cep.service.HttpApiServerTest;

/** 全部自动化测试的入口：java -cp ... cep.test.AllTests */
public final class AllTests {

    private AllTests() {}

    public static void main(String[] args) {
        TestFramework tf = new TestFramework();
        JsonParserTest.register(tf);
        HandComputedScenarioTest.register(tf);
        EnginePolicyTest.register(tf);
        TotalOrderAndTimeoutTest.register(tf);
        LateDataTest.register(tf);
        BruteForceEquivalenceTest.register(tf);
        HttpApiServerTest.register(tf);
        int code = tf.run();
        System.exit(code);
    }
}
