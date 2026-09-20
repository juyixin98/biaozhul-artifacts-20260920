package com.example.asset.repository;

import com.example.asset.domain.AssetAdjustment;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface AssetAdjustmentRepository extends JpaRepository<AssetAdjustment, Long> {

    List<AssetAdjustment> findByAssetIdOrderByIdAsc(Long assetId);
}
