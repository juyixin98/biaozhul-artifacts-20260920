package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// IssuerRef points at the local CA key material held in a Secret.
// The Secret must contain tls.crt (CA PEM) and tls.key (EC/RSA PEM).
type IssuerRef struct {
	// Name of the Secret carrying the local test CA.
	Name string `json:"name"`
}

// CertificateSpec defines the desired certificate.
type CertificateSpec struct {
	// DNSNames is the list of DNS subject alternative names. At least one required.
	// +kubebuilder:validation:MinItems=1
	DNSNames []string `json:"dnsNames"`

	// SecretName is the target Secret of type kubernetes.io/tls.
	// +kubebuilder:validation:MinLength=1
	SecretName string `json:"secretName"`

	// Duration is the requested certificate validity. e.g. "24h", "720h".
	// +optional
	Duration *metav1.Duration `json:"duration,omitempty"`

	// RenewBefore is how long before expiry renewal starts. Defaults to 1/3 of Duration.
	// +optional
	RenewBefore *metav1.Duration `json:"renewBefore,omitempty"`

	// Issuer references the local CA Secret.
	Issuer IssuerRef `json:"issuer"`
}

// CertificateStatusType is a status condition type.
type CertificateStatusType string

const (
	// ConditionReady reports whether a valid cert chain is currently served.
	ConditionReady CertificateStatusType = "Ready"
	// ConditionIssuing reports an in-progress issuance/renewal.
	ConditionIssuing CertificateStatusType = "Issuing"
)

// Well-known condition reasons.
const (
	ReasonIssued           = "Issued"
	ReasonRenewed          = "Renewed"
	ReasonReused           = "ReusedPending"
	ReasonPending          = "Pending"
	ReasonIssuanceFailed   = "IssuanceFailed"
	ReasonValidationFailed = "ValidationFailed"
	ReasonSpecInvalid      = "SpecInvalid"
	ReasonCAUnavailable    = "CAUnavailable"
)

// CertificateStatus defines the observed state.
type CertificateStatus struct {
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
	// +optional
	NotBefore metav1.Time `json:"notBefore,omitempty"`
	// +optional
	NotAfter metav1.Time `json:"notAfter,omitempty"`
	// Serial is the certificate serial number in colon-hex.
	// +optional
	Serial string `json:"serial,omitempty"`
	// Thumbprint is the SHA-256 fingerprint of the DER leaf certificate (colon-hex).
	// +optional
	Thumbprint string `json:"thumbprint,omitempty"`
	// SpecHash is the SHA-256 of the effective spec (dnsNames/duration/renewBefore/issuer).
	// +optional
	SpecHash string `json:"specHash,omitempty"`
	// CurrentSecret holds the name of the Secret serving the current chain.
	// +optional
	CurrentSecret string `json:"currentSecret,omitempty"`
	// RenewalTime is when the controller will renew the certificate.
	// +optional
	RenewalTime *metav1.Time `json:"renewalTime,omitempty"`
	// Conditions are the latest observations.
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status

// Certificate is the Schema for the certificates API.
type Certificate struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   CertificateSpec   `json:"spec,omitempty"`
	Status CertificateStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// CertificateList contains a list of Certificate.
type CertificateList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Certificate `json:"items"`
}

// DeepCopyInto copies the receiver into out.
func (in *Certificate) DeepCopyInto(out *Certificate) {
	*out = *in
	out.TypeMeta = in.TypeMeta
	in.ObjectMeta.DeepCopyInto(&out.ObjectMeta)
	in.Spec.DeepCopyInto(&out.Spec)
	in.Status.DeepCopyInto(&out.Status)
}

// DeepCopy returns a deep copy of the receiver.
func (in *Certificate) DeepCopy() *Certificate {
	if in == nil {
		return nil
	}
	out := new(Certificate)
	in.DeepCopyInto(out)
	return out
}

// DeepCopyObject returns a generically typed deep copy.
func (in *Certificate) DeepCopyObject() runtime.Object {
	if c := in.DeepCopy(); c != nil {
		return c
	}
	return nil
}

// DeepCopyInto for CertificateList.
func (in *CertificateList) DeepCopyInto(out *CertificateList) {
	*out = *in
	out.TypeMeta = in.TypeMeta
	in.ListMeta.DeepCopyInto(&out.ListMeta)
	if in.Items != nil {
		in, out := &in.Items, &out.Items
		*out = make([]Certificate, len(*in))
		for i := range *in {
			(*in)[i].DeepCopyInto(&(*out)[i])
		}
	}
}

// DeepCopy returns a deep copy.
func (in *CertificateList) DeepCopy() *CertificateList {
	if in == nil {
		return nil
	}
	out := new(CertificateList)
	in.DeepCopyInto(out)
	return out
}

// DeepCopyObject returns a generically typed deep copy.
func (in *CertificateList) DeepCopyObject() runtime.Object {
	if c := in.DeepCopy(); c != nil {
		return c
	}
	return nil
}

// DeepCopyInto copies the spec.
func (in *CertificateSpec) DeepCopyInto(out *CertificateSpec) {
	*out = *in
	if in.DNSNames != nil {
		out.DNSNames = append([]string(nil), in.DNSNames...)
	}
	if in.Duration != nil {
		out.Duration = &metav1.Duration{Duration: in.Duration.Duration}
	}
	if in.RenewBefore != nil {
		out.RenewBefore = &metav1.Duration{Duration: in.RenewBefore.Duration}
	}
}

// DeepCopyInto copies the status.
func (in *CertificateStatus) DeepCopyInto(out *CertificateStatus) {
	*out = *in
	if in.RenewalTime != nil {
		t := *in.RenewalTime
		out.RenewalTime = &t
	}
	if in.Conditions != nil {
		out.Conditions = make([]metav1.Condition, len(in.Conditions))
		for i := range in.Conditions {
			in.Conditions[i].DeepCopyInto(&out.Conditions[i])
		}
	}
}

func init() {
	SchemeBuilder.Register(&Certificate{}, &CertificateList{})
}
