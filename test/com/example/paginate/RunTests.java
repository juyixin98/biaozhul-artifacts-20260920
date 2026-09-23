package com.example.paginate;

/**
 * 测试入口：先跑直接单元测试，再启动真实 HTTP 服务跑端到端测试。
 * 用法：java -cp build/classes:build/test-classes com.example.paginate.RunTests
 */
public final class RunTests {

    public static void main(String[] args) throws Exception {
        TestFramework unit = new TestFramework("单元测试");
        UnitTests.register(unit);
        int unitCode = unit.summary();

        System.out.println();
        TestFramework integration = new TestFramework("HTTP 端到端测试");
        int port = IntegrationTests.register(integration);
        System.out.println("（测试服务监听端口 " + port + "，TTL=1s）");
        int integrationCode = integration.summary();

        System.exit(unitCode == 0 && integrationCode == 0 ? 0 : 1);
    }

    private RunTests() {
    }
}
