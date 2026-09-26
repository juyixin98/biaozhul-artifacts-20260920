package mesh

import (
	"errors"
	"net/http"
	"testing"
	"time"

	"retrybudget/internal/retry"
)

func TestParseRetryAfterDeltaSeconds(t *testing.T) {
	d, ok := parseRetryAfter("3")
	if !ok || d != 3*time.Second {
		t.Fatalf("delta-seconds: %v/%v", d, ok)
	}
}

func TestParseRetryAfterHTTPDate(t *testing.T) {
	future := time.Now().Add(2 * time.Second).UTC().Format(http.TimeFormat)
	d, ok := parseRetryAfter(future)
	if !ok {
		t.Fatal("HTTP-date should parse")
	}
	if d < time.Second || d > 3*time.Second {
		t.Fatalf("date duration=%s, want ~2s", d)
	}
}

func TestParseRetryAfterGarbage(t *testing.T) {
	if _, ok := parseRetryAfter("soon"); ok {
		t.Fatal("garbage must not parse")
	}
	if _, ok := parseRetryAfter(""); ok {
		t.Fatal("empty must not parse")
	}
}

func TestClassify503IsRetryable(t *testing.T) {
	resp := &http.Response{StatusCode: http.StatusServiceUnavailable, Header: http.Header{}}
	if retry.Classify(classifyResponse(resp, nil)) != retry.KindRetryable {
		t.Fatal("503 must classify retryable")
	}
}

func TestClassify429WithRetryAfterCarriesHint(t *testing.T) {
	resp := &http.Response{
		StatusCode: http.StatusTooManyRequests,
		Header:     http.Header{"Retry-After": []string{"5"}},
	}
	err := classifyResponse(resp, nil)
	if retry.Classify(err) != retry.KindRetryable {
		t.Fatal("429 must classify retryable")
	}
	var ce *retry.ClassifiedError
	if !errors.As(err, &ce) || !ce.HasRetryAfter || ce.RetryAfter != 5*time.Second {
		t.Fatalf("error=%v, expected RetryAfter 5s", err)
	}
}

func TestClassify400IsNonRetryable(t *testing.T) {
	resp := &http.Response{StatusCode: http.StatusBadRequest, Header: http.Header{}}
	if retry.Classify(classifyResponse(resp, nil)) != retry.KindNonRetryable {
		t.Fatal("400 must classify non-retryable")
	}
}

func TestClassify2xxIsSuccess(t *testing.T) {
	resp := &http.Response{StatusCode: http.StatusOK, Header: http.Header{}}
	if err := classifyResponse(resp, nil); err != nil {
		t.Fatalf("200 err=%v, want nil", err)
	}
}

func TestClassifyBudgetVerdictIsTerminal(t *testing.T) {
	for _, tc := range []struct {
		verdict string
		want    retry.Kind
	}{
		{VerdictBudgetExhausted, retry.KindBudgetExhausted},
		{VerdictDeadline, retry.KindDeadlineExceeded},
		{VerdictNonRetryable, retry.KindNonRetryable},
	} {
		resp := &http.Response{
			StatusCode: http.StatusServiceUnavailable,
			Header:     http.Header{headerVerdict: []string{tc.verdict}},
		}
		if got := retry.Classify(classifyResponse(resp, nil)); got != tc.want {
			t.Fatalf("verdict=%s kind=%s, want %s", tc.verdict, got, tc.want)
		}
	}
}

func TestClassifyLocalExhaustedKind(t *testing.T) {
	resp := &http.Response{
		StatusCode: http.StatusServiceUnavailable,
		Header:     http.Header{headerVerdict: []string{VerdictLocalExhausted}},
	}
	if got := retry.Classify(classifyResponse(resp, nil)); got != retry.KindLocalExhausted {
		t.Fatalf("kind=%s, want local-exhausted", got)
	}
}
