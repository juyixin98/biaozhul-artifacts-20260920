package com.example.itasset.repo;

import com.example.itasset.domain.DepreciationEntry;
import jakarta.persistence.LockModeType;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

public interface DepreciationEntryRepository extends JpaRepository<DepreciationEntry, Long> {

    List<DepreciationEntry> findByAssetIdOrderByPeriodAsc(Long assetId);

    List<DepreciationEntry> findByPeriodOrderByAssetIdAsc(String period);

    @Modifying
    @Transactional
    int deleteByAssetIdAndPeriodGreaterThanEqual(Long assetId, String fromPeriod);

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select e from DepreciationEntry e where e.assetId = :assetId and e.period >= :fromPeriod order by e.period")
    List<DepreciationEntry> lockOpenEntries(@Param("assetId") Long assetId, @Param("fromPeriod") String fromPeriod);
}
