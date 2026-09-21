package com.example.itasset.repo;

import com.example.itasset.domain.DepreciationEntry;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;

public interface DepreciationEntryRepository extends JpaRepository<DepreciationEntry, Long> {

    Optional<DepreciationEntry> findByAssetIdAndPeriod(Long assetId, Integer period);

    boolean existsByAssetIdAndPeriod(Long assetId, Integer period);

    List<DepreciationEntry> findByAssetIdOrderByPeriodAsc(Long assetId);

    List<DepreciationEntry> findByPeriodOrderByAssetIdAsc(Integer period);

    /** 该资产在给定期间之前是否已存在计提分录（顺序性校验用）。 */
    boolean existsByAssetIdAndPeriodLessThan(Long assetId, Integer period);
}
