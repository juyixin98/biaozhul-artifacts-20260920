package com.example.asset;

import com.example.asset.domain.Asset;
import com.example.asset.domain.AssetStatus;
import com.example.asset.domain.DepreciationMethod;
import com.example.asset.repository.AssetRepository;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.TestInstance;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.web.servlet.MockMvc;
import org.testcontainers.containers.MySQLContainer;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.UUID;

/**
 * 集成测试基类：Testcontainers 启动真实 MySQL 8.4，Flyway 仅跑结构迁移（不含样例数据）。
 */
@SpringBootTest(properties = "spring.flyway.locations=classpath:db/migration")
@AutoConfigureMockMvc
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
public abstract class AbstractIntegrationTest {

    static final MySQLContainer<?> MYSQL = new MySQLContainer<>("mysql:8.4.0")
            .withDatabaseName("assetdb")
            .withUsername("asset")
            .withPassword("asset123");

    static {
        MYSQL.start();
    }

    @DynamicPropertySource
    static void datasourceProps(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", MYSQL::getJdbcUrl);
        registry.add("spring.datasource.username", MYSQL::getUsername);
        registry.add("spring.datasource.password", MYSQL::getPassword);
    }

    @Autowired
    protected MockMvc mvc;
    @Autowired
    protected ObjectMapper om;
    @Autowired
    protected AssetRepository assetRepository;

    protected Asset newAsset(String cost, String salvage, int lifeMonths,
                           DepreciationMethod method, AssetStatus status, String commissionDate) {
        Asset a = new Asset();
        a.setAssetCode("T-" + UUID.randomUUID().toString().substring(0, 8));
        a.setName("测试资产");
        a.setPurchaseCost(new BigDecimal(cost));
        a.setSalvageValue(new BigDecimal(salvage));
        a.setCommissionDate(LocalDate.parse(commissionDate));
        a.setUsefulLifeMonths(lifeMonths);
        a.setDepartment("测试部");
        a.setStatus(status);
        a.setDepreciationMethod(method);
        return assetRepository.save(a);
    }
}
