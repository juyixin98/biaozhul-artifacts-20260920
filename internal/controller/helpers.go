package controller

import (
	"strings"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// permanentCSRSubstrings marks CSR-content failures that cannot be fixed by a
// retry of the same request (bad SANs, bad signature, wrong subject).
var permanentCSRSubstrings = []string{
	"CSR SAN mismatch",
	"CSR common name mismatch",
	"verify CSR signature",
	"parse CSR",
	"CSR is not PEM",
	"invalid validity window",
}

func isCSRValidationError(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	for _, s := range permanentCSRSubstrings {
		if strings.Contains(msg, s) {
			return true
		}
	}
	return false
}

// errPermanentCSR lets test signers force the permanent path.
var errPermanentCSR = permanentError("permanent CSR error")

type permanentError string

func (e permanentError) Error() string { return string(e) }

func metav1Time(t time.Time) metav1.Time { return metav1.NewTime(t) }

func ptrTime(t time.Time) *metav1.Time {
	v := metav1.NewTime(t)
	return &v
}
