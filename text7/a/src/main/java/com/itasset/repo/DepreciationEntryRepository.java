package com.itasset.repo;

import com.itasset.domain.DepreciationEntry;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;
import java.util.Optional;

public interface DepreciationEntryRepository extends JpaRepository<DepreciationEntry, Long> {

    Optional<DepreciationEntry> findByAssetIdAndPeriod(Long assetId, String period);

    boolean existsByAssetIdAndPeriod(Long assetId, String period);

    List<DepreciationEntry> findByAssetIdOrderByPeriodAsc(Long assetId);

    @Query("select e from DepreciationEntry e where e.assetId = :assetId and e.period >= :period")
    List<DepreciationEntry> findFromPeriod(@Param("assetId") Long assetId, @Param("period") String period);

    List<DepreciationEntry> findByPeriodOrderByAssetIdAsc(String period);

    @Query("select coalesce(max(e.period), '') from DepreciationEntry e where e.assetId = :assetId")
    String findMaxPeriod(@Param("assetId") Long assetId);

    long countByAssetId(Long assetId);
}
