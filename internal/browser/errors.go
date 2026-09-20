package browser

import (
	"context"
	"errors"
	"fmt"
)

// Failure codes recorded on failed runs. They are deliberately distinct so
// navigation timeout, browser exit and per-resource failure can never be
// conflated.
const (
	CodePolicyBlocked     = "POLICY_BLOCKED"     // document URL rejected by scheme/whitelist policy
	CodeRedirectLimit     = "REDIRECT_LIMIT"     // main-document redirect chain exceeded configured bound
	CodeNavigationTimeout = "NAVIGATION_TIMEOUT" // load event did not arrive within NAV_TIMEOUT
	CodeNavigationFailed  = "NAVIGATION_FAILED"  // main document network/protocol error
	CodeBrowserCrash      = "BROWSER_CRASH"      // chromium process exited unexpectedly
	CodeTaskCancelled     = "TASK_CANCELLED"     // worker shutdown / lease cancelled the run
	CodeMetricHarvest     = "METRIC_HARVEST_FAILED"
	CodeBrowserLaunch     = "BROWSER_LAUNCH_FAILED"
	CodeInternal          = "INTERNAL_ERROR"
)

// CollectError is the structured failure the worker maps onto a failed run.
type CollectError struct {
	Code    string
	Message string
}

func (e *CollectError) Error() string { return fmt.Sprintf("%s: %s", e.Code, e.Message) }

// AsCollectError extracts a *CollectError from an error chain.
func AsCollectError(err error) (*CollectError, bool) {
	var ce *CollectError
	if errors.As(err, &ce) {
		return ce, true
	}
	return nil, false
}

func fail(code, format string, args ...interface{}) *CollectError {
	return &CollectError{Code: code, Message: fmt.Sprintf(format, args...)}
}

var _ = context.Background
