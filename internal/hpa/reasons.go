package hpa

// Default policy values (mirror Kubernetes HPA conventions).
const (
	DefaultTargetUtilization  = 70
	DefaultTolerancePct       = 10.0
	DefaultMinReplicas        = 1
	DefaultMaxReplicas        = 10
	DefaultScaleDownWindowSec = 300 // kube-controller-manager --horizontal-pod-autoscaler-downscale-stabilization
	DefaultScaleUpWindowSec   = 0   // HPA upscaling acts immediately
	DefaultFreshnessSec       = 120
)

// Reason codes. Raw reasons describe the formula; final reasons additionally
// describe the stable window, clamping and data-quality handling.
const (
	// formula
	ReasonRawScaleUp    = "raw_scale_up"
	ReasonRawScaleDown  = "raw_scale_down"
	ReasonToleranceHold = "tolerance_band_hold"
	ReasonNoChange      = "exact_target_hold"
	// data quality (no recommendation is recorded)
	ReasonZeroTargetValue  = "zero_target_value_rejected"
	ReasonMetricsStale     = "metrics_stale"
	ReasonNoInstances      = "no_instances"
	ReasonNoReadyInstances = "no_ready_instances"
	ReasonAllReadyMissing  = "all_ready_instances_missing_metrics"
	// window
	ReasonScaleDownWindowMax = "scale_down_stable_window_max"
	ReasonScaleUpWindowMax   = "scale_up_stable_window_max"
	ReasonNoWindow           = "no_stable_window"
	ReasonConfigVersionReset = "config_version_reset_clears_window"
	// clamping
	ReasonCappedAtMax    = "capped_at_max_replicas"
	ReasonCappedAtMin    = "capped_at_min_replicas"
	ReasonFloorAtMinZero = "current_zero_floored_to_min"
)
