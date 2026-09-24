package webhook

import (
	"encoding/json"
	"net/http"

	admissionv1 "k8s.io/api/admission/v1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/serializer"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
)

// groupName is the single API group every webhook in this server serves.
const groupName = "migration.example.io"

// webhookScheme knows apiextensions.k8s.io/v1 ConversionReview and
// admission.k8s.io/v1 AdmissionReview. It never registers our own CRD types:
// request payloads are processed as raw JSON so fields unknown to a typed
// representation are not dropped.
var (
	webhookScheme    = runtime.NewScheme()
	universalDecoder runtime.Decoder
	admissionDecoder runtime.Decoder
)

func init() {
	utilruntime.Must(apiextensionsv1.AddToScheme(webhookScheme))
	utilruntime.Must(admissionv1.AddToScheme(webhookScheme))
	codecs := serializer.NewCodecFactory(webhookScheme)
	universalDecoder = codecs.UniversalDeserializer()
	admissionDecoder = universalDecoder
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	_ = enc.Encode(v)
}
