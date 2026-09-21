package com.example.itasset;

import com.example.itasset.support.ApiClient;
import org.junit.jupiter.api.BeforeEach;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.MySQLContainer;
import org.testcontainers.utility.DockerImageName;

/**
 * Boots the whole app against a real MySQL 8.4 container. Every test class gets a fresh
 * database; Flyway runs the same migrations used in production and seeds the sample data.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
public abstract class AbstractIntegrationTest {

    static final MySQLContainer<?> MYSQL;

    static {
        MYSQL = new MySQLContainer<>(DockerImageName.parse("mysql:8.4"))
                .withDatabaseName("itasset_test")
                .withUsername("test")
                .withPassword("test")
                .withUrlParam("serverTimezone", "Asia/Shanghai")
                .withReuse(false);
        MYSQL.start();
    }

    @DynamicPropertySource
    static void props(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", MYSQL::getJdbcUrl);
        registry.add("spring.datasource.username", MYSQL::getUsername);
        registry.add("spring.datasource.password", MYSQL::getPassword);
        registry.add("spring.jpa.hibernate.ddl-auto", () -> "validate");
        // Pin the accounting clock so tests own disjoint (future) period windows.
        registry.add("app.clock.fixed", () -> "2031-06-30");
    }

    @LocalServerPort
    protected int port;

    protected ApiClient api;

    @BeforeEach
    void initClient() {
        api = new ApiClient(baseUrl());
    }

    protected String baseUrl() {
        return "http://localhost:" + port;
    }
}
