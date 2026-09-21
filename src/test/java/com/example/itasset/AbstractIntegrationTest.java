package com.example.itasset;

import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.MySQLContainer;

/**
 * 真实 MySQL 8 集成测试基类：
 * <ul>
 *   <li>默认（Docker 可用）：Testcontainers 启动一次性 mysql:8.4，Flyway 与应用
 *       完整执行迁移；</li>
 *   <li>外部库模式：设置 IT_MYSQL_URL / IT_MYSQL_USER / IT_MYSQL_PASSWORD
 *       （可选 IT_MYSQL_CATALOG）后直接连接已有 MySQL，不触碰 Docker。</li>
 * </ul>
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
public abstract class AbstractIntegrationTest {

    static final boolean EXTERNAL_DB = System.getenv("IT_MYSQL_URL") != null;

    static final MySQLContainer<?> MYSQL;

    static {
        if (EXTERNAL_DB) {
            MYSQL = null;
        } else {
            MYSQL = new MySQLContainer<>("mysql:8.4")
                    .withDatabaseName("itasset_test")
                    .withUsername("itest")
                    .withPassword("itestpass")
                    .withCommand("--character-set-server=utf8mb4",
                            "--collation-server=utf8mb4_unicode_ci",
                            "--transaction-isolation=READ-COMMITTED")
                    .withReuse(true);
            MYSQL.start();
        }
    }

    @DynamicPropertySource
    static void datasourceProps(DynamicPropertyRegistry registry) {
        if (EXTERNAL_DB) {
            registry.add("spring.datasource.url", () -> System.getenv("IT_MYSQL_URL"));
            registry.add("spring.datasource.username", () -> System.getenv("IT_MYSQL_USER"));
            registry.add("spring.datasource.password", () -> System.getenv("IT_MYSQL_PASSWORD"));
            String catalog = System.getenv("IT_MYSQL_CATALOG");
            if (catalog != null) {
                registry.add("spring.datasource.catalog", () -> catalog);
            }
        } else {
            registry.add("spring.datasource.url", MYSQL::getJdbcUrl);
            registry.add("spring.datasource.username", MYSQL::getUsername);
            registry.add("spring.datasource.password", MYSQL::getPassword);
        }
        registry.add("app.seed-sample", () -> "false");
    }
}
