package integration

import (
	"context"
	"crypto/tls"
	"net/http"
	"net/url"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	apiextensionsclient "k8s.io/apiextensions-apiserver/pkg/client/clientset/clientset"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"

	migconv "github.com/example/crd-migration-compat/internal/conversion"
	migwebhook "github.com/example/crd-migration-compat/internal/webhook"
)

// TestConversionTimeoutEndToEnd flips the CRD's conversion endpoint at a
// deliberately slow in-process TLS server for the duration of the test and
// verifies the API server aborts the conversion within its webhook timeout
// budget instead of hanging or returning truncated data.
//
// It runs only when RUN_SLOW_TESTS=1 so the default acceptance loop stays
// fast; the per-object deadline itself is covered deterministically by the
// unit tests (TestConversionHandlerTimeout / TestTimeoutHonoursContext).
func TestConversionTimeoutEndToEnd(t *testing.T) {
	if slowTestsEnabled() == "" {
		t.Skip("slow end-to-end timeout test; set RUN_SLOW_TESTS=1 to enable")
	}

	ctx := context.Background()
	cs, err := apiextensionsclient.NewForConfig(restCfg)
	require.NoError(t, err)

	// Seed a stored v1 object up front, independent of the other tests.
	seed := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "migration.example.io/v1",
		"kind":       "Task",
		"metadata":   map[string]any{"name": "slow-seed", "namespace": "itest"},
		"spec":       map[string]any{"timeout": map[string]any{"seconds": int64(5)}},
	}}
	_, err = dynamicClient(t).Resource(gvrV1).Namespace("itest").Create(ctx, seed, metav1.CreateOptions{})
	require.NoError(t, err)

	// Start a second TLS webhook whose converter blocks ~45s per object,
	// longer than the apiserver's hard 30s timeout for CRD conversion
	// webhooks (that timeout is fixed: CRD conversion has no configurable
	// timeoutSeconds, unlike admission webhooks' 10s default).
	slowCtx, stopSlow := context.WithCancel(context.Background())
	t.Cleanup(stopSlow)
	slowURL := startSlowWebhook(t, slowCtx)

	// Point the CRD conversion clientConfig at the slow server and restore
	// the original endpoint afterwards.
	crd, err := cs.ApiextensionsV1().CustomResourceDefinitions().Get(ctx, "tasks.migration.example.io", metav1.GetOptions{})
	require.NoError(t, err)
	originalCC := crd.Spec.Conversion.Webhook.ClientConfig.DeepCopy()
	crd.Spec.Conversion.Webhook.ClientConfig = &apiextensionsv1.WebhookClientConfig{
		URL:      ptrString(slowURL),
		CABundle: testEnv.WebhookInstallOptions.LocalServingCAData,
	}
	_, err = cs.ApiextensionsV1().CustomResourceDefinitions().Update(ctx, crd, metav1.UpdateOptions{})
	require.NoError(t, err)
	t.Cleanup(func() {
		cur, gErr := cs.ApiextensionsV1().CustomResourceDefinitions().Get(context.Background(), "tasks.migration.example.io", metav1.GetOptions{})
		if gErr != nil {
			return
		}
		cur.Spec.Conversion.Webhook.ClientConfig = originalCC
		_, _ = cs.ApiextensionsV1().CustomResourceDefinitions().Update(context.Background(), cur, metav1.UpdateOptions{})
	})

	// Reading the object as v1alpha1 requires a server-side v1->v1alpha1
	// conversion against the slow endpoint. The apiserver must give up at
	// its hard 30s conversion-webhook deadline rather than waiting 45s.
	dyn := dynamicClient(t)
	start := time.Now()
	_, err = dyn.Resource(schema.GroupVersionResource{
		Group: "migration.example.io", Version: "v1alpha1", Resource: "tasks",
	}).Namespace("itest").Get(ctx, "slow-seed", metav1.GetOptions{})
	elapsed := time.Since(start)
	require.Error(t, err)
	// The CRD conversion webhook hard timeout is 30s. Assert the apiserver
	// gave up in that window rather than waiting for the 45s hook, while
	// tolerating scheduling slack. This documents the real platform limit —
	// the tighter per-object deadline is enforced by the webhook itself
	// (--conversion-timeout) and covered by the fast unit tests.
	assert.Greater(t, elapsed, 25*time.Second, "apiserver should wait until its 30s conversion deadline")
	assert.Less(t, elapsed, 40*time.Second,
		"apiserver must abort at its 30s conversion deadline rather than waiting 45s (took %v)", elapsed)
	msg := strings.ToLower(err.Error())
	assert.True(t, strings.Contains(msg, "webhook") || strings.Contains(msg, "deadline") ||
		strings.Contains(msg, "timeout") || strings.Contains(msg, "context") ||
		strings.Contains(msg, "internal error"),
		"expected a webhook-timeout error, got: %v", err)
}

// startSlowWebhook runs a bare TLS server (no controller-runtime manager, so
// no metrics listener can collide) serving the same mux with a converter that
// blocks past the apiserver webhook budget. It returns the /convert URL.
func startSlowWebhook(t *testing.T, ctx context.Context) string {
	t.Helper()
	slow := migconv.NewConverter(migconv.Hooks{
		PreConvert: func(ctx context.Context, _ migconv.Direction, _ string) error {
			select {
			case <-time.After(45 * time.Second):
			case <-ctx.Done():
			}
			return ctx.Err()
		},
	})
	mux := migwebhook.NewMux(migwebhook.Options{Converter: slow, ConversionTimeout: 45 * time.Second})

	port := freeLocalPort(t)
	certFile := filepath.Join(testEnv.WebhookInstallOptions.LocalServingCertDir, "tls.crt")
	keyFile := filepath.Join(testEnv.WebhookInstallOptions.LocalServingCertDir, "tls.key")
	srv := &http.Server{ //#nosec G112 -- test-only endpoint
		Addr:    netJoinHostPort("127.0.0.1", port),
		Handler: mux,
	}
	go func() {
		if err := srv.ListenAndServeTLS(certFile, keyFile); err != nil && err != http.ErrServerClosed {
			t.Logf("slow webhook exited: %v", err)
		}
	}()
	t.Cleanup(func() {
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdownCtx)
	})

	host := testEnv.WebhookInstallOptions.LocalServingHost
	if host == "" {
		host = "127.0.0.1"
	}
	base := &url.URL{Scheme: "https", Host: netJoinHostPort(host, port)}
	require.Eventually(t, func() bool {
		client := &http.Client{Timeout: 500 * time.Millisecond, Transport: &http.Transport{
			TLSClientConfig: &tls.Config{InsecureSkipVerify: true}, //#nosec G402 -- test endpoint
		}}
		resp, err := client.Get(base.String() + "/healthz")
		if err != nil {
			return false
		}
		_ = resp.Body.Close()
		return resp.StatusCode == http.StatusOK
	}, 10*time.Second, 100*time.Millisecond)
	return base.String() + "/convert"
}
