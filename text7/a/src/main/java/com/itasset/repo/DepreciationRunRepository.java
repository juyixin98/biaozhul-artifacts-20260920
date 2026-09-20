package com.itasset.repo;

import com.itasset.domain.DepreciationRun;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Optional;

public interface DepreciationRunRepository extends JpaRepository<DepreciationRun, Long> {
    Optional<DepreciationRun> findByRequestId(String requestId);
}
