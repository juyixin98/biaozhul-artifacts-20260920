package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// IssuerRef identifies the test CA Secret that signs certificates.
type IssuerRef struct {
	// Name is the name of a kubernetes.io/tls Secret that contains the
	// CA certificate (ca.crt / tls.crt) and private key (tls.key).
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
}

// CertificateSpec is the desired state of a Certificate.
type CertificateSpec struct {
	// DNSNames is the list of DNS subjectAltNames. At least one entry is
	// required.
	// +kubebuilder:validation:MinItems=1
	DNSNames []string `json:"dnsNames"`

	// CommonName is the X.509 subject common name. Optional.
	// +optional
	CommonName string `json:"commonName,omitempty"`

	// Duration is the validity period of the issued certificate.
	// Defaults to 24h. Must be >= 10m.
	// +optional
	Duration *metav1.Duration `json:"duration,omitempty"`

	// RenewBefore is how long before expiry a new certificate is requested.
	// Defaults to one third of Duration. It must be strictly smaller than
	// Duration: the renewal window is always derived from the certificate's
	// own validity period, never an absolute wall-clock schedule.
	// +optional
	RenewBefore *metav1.Duration `json:"renewBefore,omitempty"`

	// SecretName is the target TLS Secret in the Certificate's namespace.
	// +kubebuilder:validation:MinLength=1
	SecretName string `json:"secretName"`

	// IssuerRef selects the test CA Secret used for signing.
	IssuerRef IssuerRef `json:"issuerRef"`
}

// CertificateStatus is the observed state of a Certificate.
type CertificateStatus struct {
	// ObservedGeneration is the metadata.generation last reconciled.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// NotAfter is the expiry of the certificate currently in the Secret.
	// +optional
	NotAfter *metav1.Time `json:"notAfter,omitempty"`

	// NotBefore is the validity start of the certificate currently in the Secret.
	// +optional
	NotBefore *metav1.Time `json:"notBefore,omitempty"`

	// RenewalTime is the point in time at which the certificate enters its
	// renewal window (NotAfter - RenewBefore, capped by NotBefore).
	// +optional
	RenewalTime *metav1.Time `json:"renewalTime,omitempty"`

	// SerialNumber is the hex encoded serial of the certificate currently in
	// the Secret (":"-separated bytes, matching x509.Certificate.SerialNumber
	// text format).
	// +optional
	SerialNumber string `json:"serialNumber,omitempty"`

	// SpecHash identifies the CertificateRequest input set
	// (DNSNames + CommonName + Duration). The controller refuses to reuse
	// CertificateRequests whose hash does not match the live spec, so an
	// old-generation issuance receipt can never overwrite a new domain
	// configuration.
	// +optional
	SpecHash string `json:"specHash,omitempty"`

	// Conditions contains the observed conditions of the Certificate.
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// Condition types and reasons shared by Certificate and CertificateRequest.
const (
	// ConditionReady reports whether the Secret contains a valid, in-spec
	// certificate chain.
	ConditionReady = "Ready"

	ReasonReconciled     = "Reconciled"
	ReasonRenewing       = "Renewing"
	ReasonInvalidSpec    = "InvalidSpec"
	ReasonMissingSecret  = "MissingSecret"
	ReasonInvalidSecret  = "InvalidSecret"
	ReasonIssuing        = "Issuing"
	ReasonIssuanceFailed = "IssuanceFailed"
	ReasonPending        = "Pending"
	ReasonSigned         = "Signed"
	ReasonCAUnavailable  = "CAUnavailable"
	ReasonCSRInvalid     = "CSRInvalid"
)

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=cert
// +kubebuilder:printcolumn:name="Secret",type=string,JSONPath=`.spec.secretName`
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=`.status.conditions[?(@.type=="Ready")].status`
// +kubebuilder:printcolumn:name="NotAfter",type=date,JSONPath=`.status.notAfter`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// Certificate declares a TLS certificate that is continuously kept valid by
// the renewal coordinator.
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

// CertificateRequestSpec is the desired state of a CertificateRequest.
type CertificateRequestSpec struct {
	// Request is a PEM-encoded PKCS#10 certificate signing request.
	// +kubebuilder:validation:MinLength=1
	Request []byte `json:"request"`

	// Duration is the requested validity period.
	Duration metav1.Duration `json:"duration"`

	// IssuerRef selects the test CA Secret used for signing.
	IssuerRef IssuerRef `json:"issuerRef"`
}

// CertificateRequestStatus is the observed state of a CertificateRequest.
type CertificateRequestStatus struct {
	// Certificate is the PEM-encoded signed certificate once signing succeeds.
	// +optional
	Certificate []byte `json:"certificate,omitempty"`

	// CA is the PEM-encoded CA certificate that signed Certificate, once
	// signing succeeds.
	// +optional
	CA []byte `json:"ca,omitempty"`

	// SerialNumber is the hex encoded serial of the signed certificate.
	// +optional
	SerialNumber string `json:"serialNumber,omitempty"`

	// NotAfter / NotBefore report the signed certificate validity.
	// +optional
	NotAfter *metav1.Time `json:"notAfter,omitempty"`
	// +optional
	NotBefore *metav1.Time `json:"notBefore,omitempty"`

	// FailureCount tracks consecutive signer failures for backoff.
	// +optional
	FailureCount int32 `json:"failureCount,omitempty"`

	// Conditions contains the observed conditions of the CertificateRequest.
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=crq
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=`.status.conditions[?(@.type=="Ready")].status`
// +kubebuilder:printcolumn:name="Serial",type=string,JSONPath=`.status.serialNumber`
// +kubebuilder:printcolumn:name="NotAfter",type=date,JSONPath=`.status.notAfter`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// CertificateRequest is an in-flight or completed signing request.
type CertificateRequest struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   CertificateRequestSpec   `json:"spec,omitempty"`
	Status CertificateRequestStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// CertificateRequestList contains a list of CertificateRequest.
type CertificateRequestList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []CertificateRequest `json:"items"`
}

func init() {
	SchemeBuilder.Register(&Certificate{}, &CertificateList{}, &CertificateRequest{}, &CertificateRequestList{})
}
