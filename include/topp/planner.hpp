#pragma once
#include "topp/types.hpp"
#include <Eigen/Dense>
#include <optional>
#include <vector>

namespace topp {

struct PlanningRequest {
    Eigen::MatrixXd waypoints;          // dof x n  (columns are points)
    Eigen::VectorXd vMax;               // dof
    Eigen::VectorXd aMax;               // dof
    int gridCells = 200;
    int samplesPerCell = 4;             // dense in-segment verification
    double duplicateTolerance = 1e-9;  // consecutive-identical-point threshold
    std::optional<Eigen::VectorXd> startVelocity;
};

// Full pipeline: dedupe -> natural cubic spline -> time-optimal TOPP ->
// dense in-segment sampling verification -> bottleneck interval extraction.
Result plan(const PlanningRequest& req);

} // namespace topp
