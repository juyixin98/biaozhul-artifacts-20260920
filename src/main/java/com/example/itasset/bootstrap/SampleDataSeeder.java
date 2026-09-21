package com.example.itasset.bootstrap;

import com.example.itasset.domain.AssetStatus;
import com.example.itasset.domain.DepreciationMethod;
import com.example.itasset.repo.AssetRepository;
import com.example.itasset.service.AssetLifecycleService;
import com.example.itasset.service.DepreciationService;
import com.example.itasset.web.Dtos;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.CommandLineRunner;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;

import java.math.BigDecimal;
import java.time.LocalDate;

/**
 * 样例数据（仅 app.seed-sample=true 时运行，docker-compose 默认开启）：
 * 三台资产覆盖两种折旧方法与不同账龄，并补发 2026 年内的历史计提分录。
 * 所有写入走正式服务，因此唯一约束/顺序校验同样生效；重复启动幂等。
 */
@Component
@Order(20)
public class SampleDataSeeder implements CommandLineRunner {

    private static final Logger log = LoggerFactory.getLogger(SampleDataSeeder.class);

    private final AssetRepository assets;
    private final AssetLifecycleService lifecycle;
    private final DepreciationService depreciation;

    @Value("${app.seed-sample:false}")
    private boolean seedSample;

    public SampleDataSeeder(AssetRepository assets,
                            AssetLifecycleService lifecycle,
                            DepreciationService depreciation) {
        this.assets = assets;
        this.lifecycle = lifecycle;
        this.depreciation = depreciation;
    }

    @Override
    public void run(String... args) {
        if (!seedSample || assets.count() > 0) {
            return;
        }
        log.info("写入样例资产与历史折旧...");

        // 1) 直线法笔记本：12000 元，残值 600，36 个月，2026-01 启用
        var laptop = lifecycle.create(new Dtos.CreateAssetRequest(
                "NB-2026-001", "ThinkPad X1 笔记本", "研发部",
                new BigDecimal("12000.00"), new BigDecimal("600.00"),
                LocalDate.of(2026, 1, 15), 36,
                DepreciationMethod.STRAIGHT_LINE), "manager");

        // 2) 余额递减法服务器：60000 元，残值 3000，48 个月，2026-03 启用
        var server = lifecycle.create(new Dtos.CreateAssetRequest(
                "SRV-2026-002", "机架式服务器 R750", "基础设施部",
                new BigDecimal("60000.00"), new BigDecimal("3000.00"),
                LocalDate.of(2026, 3, 1), 48,
                DepreciationMethod.DECLINING_BALANCE), "manager");

        // 3) 库存打印机：尚未启用，无折旧
        lifecycle.create(new Dtos.CreateAssetRequest(
                "PR-2026-003", "激光打印机", "行政部",
                new BigDecimal("3000.00"), new BigDecimal("150.00"),
                null, 36, DepreciationMethod.STRAIGHT_LINE), "manager");

        // 补发 2026-01 ~ 2026-08 历史分录（按顺序逐月）
        for (int period = 202601; period <= 202608; period++) {
            depreciation.postPeriod(period);
        }

        // 笔记本 2026-08 送修，演示维修状态
        var laptopFresh = lifecycle.requireAsset(laptop.getId());
        lifecycle.transition(laptop.getId(), AssetStatus.IN_REPAIR,
                new Dtos.TransitionRequest("sample-repair-001", laptopFresh.getVersion(),
                        "样例：主板故障送修"), "manager");

        log.info("样例数据完成：资产 {} 台（其中已启用 2 台、库存 1 台），历史期间 202601-202608 已计提",
                assets.count());
    }
}
