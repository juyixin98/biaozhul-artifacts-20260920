package admission

import (
	"fmt"

	admissionv1 "k8s.io/api/admission/v1"
	admissionregv1 "k8s.io/api/admissionregistration/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/runtime/serializer"
	"k8s.io/apimachinery/pkg/types"
)

// typesUID re-exports types.UID so the deny signature stays readable.
type typesUID = types.UID

func addAdmissionTypes(s *runtime.Scheme) error {
	gv := admissionv1.SchemeGroupVersion
	s.AddKnownTypes(gv, &admissionv1.AdmissionReview{})
	s.AddKnownTypes(admissionregv1.SchemeGroupVersion)
	return nil
}

func admissionCodec(s *runtime.Scheme) runtime.Decoder {
	factory := serializer.NewCodecFactory(s)
	return factory.UniversalDeserializer()
}

func parseGroupVersion(v string) (string, error) {
	gv, err := schema.ParseGroupVersion("timer.example.com/" + v)
	if err != nil {
		return "", fmt.Errorf("invalid kind version %q: %w", v, err)
	}
	return gv.Version, nil
}
