package com.itasset.repo;

import com.itasset.domain.ParameterAdjustment;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;

public interface ParameterAdjustmentRepository extends JpaRepository<ParameterAdjustment, Long> {

    List<ParameterAdjustment> findByAssetIdOrderByIdAsc(Long assetId);

    Optional<ParameterAdjustment> findByRequestId(String requestId);

    /** 取某期间适用的最近一次调整（effective_period &lt;= 该期间）。 */
    Optional<ParameterAdjustment> findTopByAssetIdAndEffectivePeriodLessThanEqualOrderByEffectivePeriodDescIdDesc(
            Long assetId, String period);
}
