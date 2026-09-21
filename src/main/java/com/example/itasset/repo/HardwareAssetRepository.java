package com.example.itasset.repo;

import com.example.itasset.domain.HardwareAsset;
import jakarta.persistence.LockModeType;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;
import java.util.Optional;

public interface HardwareAssetRepository extends JpaRepository<HardwareAsset, Long> {

    Optional<HardwareAsset> findByAssetCode(String assetCode);

    Page<HardwareAsset> findByDepartment(String department, Pageable pageable);

    /** Pessimistic row lock used by the monthly run so posting serializes per asset. */
    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select a from HardwareAsset a where a.id = :id")
    Optional<HardwareAsset> lockById(@Param("id") Long id);

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select a from HardwareAsset a order by a.id")
    List<HardwareAsset> lockAllForPosting();

    boolean existsByAssetCode(String assetCode);
}
