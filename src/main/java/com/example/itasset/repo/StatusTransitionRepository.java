package com.example.itasset.repo;

import com.example.itasset.domain.StatusTransition;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;

public interface StatusTransitionRepository extends JpaRepository<StatusTransition, Long> {

    Optional<StatusTransition> findByRequestId(String requestId);

    List<StatusTransition> findByAssetIdOrderByIdAsc(Long assetId);
}
