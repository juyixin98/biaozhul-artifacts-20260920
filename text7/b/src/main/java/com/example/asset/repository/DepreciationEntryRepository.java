package com.example.asset.repository;

import com.example.asset.domain.DepreciationEntry;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;

public interface DepreciationEntryRepository extends JpaRepository<DepreciationEntry, Long> {

    boolean existsByAssetIdAndPeriod(Long assetId, String period);

    int countByAssetId(Long assetId);

    int countByAssetIdAndPeriodLessThan(Long assetId, String period);

    Optional<DepreciationEntry> findTopByAssetIdAndPeriodLessThanOrderByPeriodDesc(Long assetId, String period);

    List<DepreciationEntry> findByPeriodOrderByAssetIdAsc(String period);

    List<DepreciationEntry> findByAssetIdOrderByPeriodAsc(Long assetId);
}
