package com.itasset;

import org.junit.jupiter.api.BeforeEach;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.testcontainers.containers.MySQLContainer;

import java.sql.DriverManager;
import java.util.UUID;

/**
 * 真实 MySQL 8.4 集成测试基类（Testcontainers）。
 *
 * <p>全套件共享一个 MySQL 容器；每个测试类调用 {@link #newDatabase()} 得到独立
 * database 并通过 {@link DynamicPropertySource} 指向它，Flyway 在各自库内迁移。
 * 类内方法间，关账这类全局状态由 {@link #cleanGlobalState()} 每方法清空
 * （资产数据用 UUID 编号天然隔离）。
 */
@SpringBootTest
public abstract class AbstractIntegrationTest {

    static final MySQLContainer<?> MYSQL = new MySQLContainer<>("mysql:8.4")
            .withDatabaseName("itasset_root")
            .withUsername("test")
            .withPassword("test")
            .withUrlParam("serverTimezone", "UTC")
            .withCommand("--innodb-lock-wait-timeout=5",
                    "--transaction-isolation=REPEATABLE-READ",
                    "--log-bin-trust-function-creators=1");

    static {
        MYSQL.start();
    }

    @Autowired
    private JdbcTemplate jdbc;

    @BeforeEach
    void cleanGlobalState() {
        jdbc.update("DELETE FROM period_close");
        jdbc.update("DELETE FROM period_mutex");
    }

    /** 创建并返回一个全新的独立数据库名（root 连接）。 */
    protected static String newDatabase() {
        String db = "it_" + UUID.randomUUID().toString().replace("-", "").substring(0, 16);
        try (var conn = DriverManager.getConnection(jdbcUrl("itasset_root"), "root", "test");
             var st = conn.createStatement()) {
            st.execute("CREATE DATABASE " + db + " DEFAULT CHARACTER SET utf8mb4");
            st.execute("GRANT ALL PRIVILEGES ON " + db + ".* TO 'test'@'%'");
            st.execute("FLUSH PRIVILEGES");
        } catch (Exception e) {
            throw new IllegalStateException("创建测试数据库失败", e);
        }
        return db;
    }

    /** 将 Spring 数据源指向该测试类专属数据库。 */
    protected static void register(DynamicPropertyRegistry registry, String db) {
        registry.add("spring.datasource.url", () -> jdbcUrl(db));
        registry.add("spring.datasource.username", () -> "test");
        registry.add("spring.datasource.password", () -> "test");
    }

    /** 基于容器根库 URL 替换库名得到目标库 JDBC URL。 */
    private static String jdbcUrl(String db) {
        return MYSQL.getJdbcUrl().replace("/itasset_root?", "/" + db + "?");
    }
}
