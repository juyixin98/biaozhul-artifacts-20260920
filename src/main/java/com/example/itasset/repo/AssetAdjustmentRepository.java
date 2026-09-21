package com.example.itasset.repo;

import com.example.itasset.domain.AssetAdjustment;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;

public interface AssetAdjustmentRepository extends JpaRepository<AssetAdjustment, Long> {

    List<AssetAdjustment> findByAssetIdOrderByIdDesc(Long assetId);

    Optional<AssetAdjustment> findByRequestId(String requestId);
}
