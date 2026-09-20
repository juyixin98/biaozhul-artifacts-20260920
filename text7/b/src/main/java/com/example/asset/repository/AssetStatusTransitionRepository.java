package com.example.asset.repository;

import com.example.asset.domain.AssetStatusTransition;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;

public interface AssetStatusTransitionRepository extends JpaRepository<AssetStatusTransition, Long> {

    Optional<AssetStatusTransition> findByRequestId(String requestId);

    List<AssetStatusTransition> findByAssetIdOrderByIdAsc(Long assetId);
}
