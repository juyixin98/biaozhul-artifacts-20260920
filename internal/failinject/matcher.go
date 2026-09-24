package failinject

import (
	"strings"
	"sync"
	"sync/atomic"

	admissionv1 "k8s.io/api/admission/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/serializer"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
)

// Scheme is the runtime scheme used to decode admission objects.
var Scheme = runtime.NewScheme()

// Codecs is the codec factory for decoding admission requests.
var Codecs = serializer.NewCodecFactory(Scheme)

func init() {
	utilruntime.Must(addAdmissionToScheme(Scheme))
}

func addAdmissionToScheme(s *runtime.Scheme) error {
	s.AddKnownTypes(admissionv1.SchemeGroupVersion, &admissionv1.AdmissionReview{})
	// ConfigMap is registered so future richer matching can decode the object.
	_ = corev1.ConfigMap{}
	return nil
}

// Matcher evaluates admission requests against the active FailPolicies. Its
// counters are the only mutable state: FirstN denial budgets are consumed
// atomically as real API requests arrive.
type Matcher struct {
	mu       sync.RWMutex
	policies map[string]*FailPolicy
	counters map[string]*int32
	// nsLabels caches namespace-name -> labels, maintained by the controller
	// from a namespace watch, because AdmissionRequest does not carry them.
	nsLabels map[string]map[string]string
}

// NewMatcher returns an empty Matcher.
func NewMatcher() *Matcher {
	return &Matcher{
		policies: map[string]*FailPolicy{},
		counters: map[string]*int32{},
		nsLabels: map[string]map[string]string{},
	}
}

// ReplacePolicies atomically swaps the active policy set. Counters for
// surviving policies are preserved (so a policy update that does not touch the
// budget does not reset it); counters for removed policies are dropped.
func (m *Matcher) ReplacePolicies(policies []FailPolicy) {
	m.mu.Lock()
	defer m.mu.Unlock()
	next := make(map[string]*FailPolicy, len(policies))
	nextCounters := make(map[string]*int32, len(policies))
	for i := range policies {
		p := policies[i]
		next[p.Name] = &p
		if c, ok := m.counters[p.Name]; ok {
			nextCounters[p.Name] = c
		} else {
			var c int32
			nextCounters[p.Name] = &c
		}
	}
	m.policies = next
	m.counters = nextCounters
}

// ReplaceNamespaces swaps the cached namespace labels.
func (m *Matcher) ReplaceNamespaces(namespaces []corev1.Namespace) {
	m.mu.Lock()
	defer m.mu.Unlock()
	next := make(map[string]map[string]string, len(namespaces))
	for i := range namespaces {
		if len(namespaces[i].Labels) > 0 {
			labels := make(map[string]string, len(namespaces[i].Labels))
			for k, v := range namespaces[i].Labels {
				labels[k] = v
			}
			next[namespaces[i].Name] = labels
		} else {
			next[namespaces[i].Name] = map[string]string{}
		}
	}
	m.nsLabels = next
}

// Decision is the result of matching one request.
type Decision struct {
	Deny    bool
	Message string
	Policy  string
}

// Evaluate checks one admission request. Only ConfigMaps are eligible.
func (m *Matcher) Evaluate(req *admissionv1.AdmissionRequest) Decision {
	if req == nil || req.Resource.Resource != "configmaps" {
		return Decision{}
	}
	m.mu.RLock()
	defer m.mu.RUnlock()
	nsLabels := m.nsLabels[req.Namespace]
	for key, p := range m.policies {
		if !actionAllowed(p.Spec.Actions, string(req.Operation)) {
			continue
		}
		if !namespaceInList(p.Spec.TargetNamespaces, req.Namespace) {
			continue
		}
		if !namespaceLabelsMatch(p.Spec.NamespaceSelector, nsLabels) {
			continue
		}
		if p.Spec.ObjectNameSubstring != "" &&
			!strings.Contains(req.Name, p.Spec.ObjectNameSubstring) {
			continue
		}
		switch p.Spec.Mode {
		case ModeFirstN:
			if atomic.LoadInt32(m.counters[key]) >= p.Spec.FirstN {
				continue
			}
			atomic.AddInt32(m.counters[key], 1)
		case ModeAlways, "":
			// always deny
		default:
			continue
		}
		msg := p.Spec.Message
		if msg == "" {
			msg = "injected failure by FailPolicy " + p.Name
		}
		return Decision{Deny: true, Message: msg, Policy: p.Name}
	}
	return Decision{}
}

// Consumed returns how many denies the named FirstN policy has consumed. It is
// used for tests and diagnostics.
func (m *Matcher) Consumed(policyName string) int32 {
	m.mu.RLock()
	defer m.mu.RUnlock()
	if c, ok := m.counters[policyName]; ok {
		return atomic.LoadInt32(c)
	}
	return 0
}

func actionAllowed(actions []string, op string) bool {
	if len(actions) == 0 {
		return op == ActionCreate
	}
	for _, a := range actions {
		if strings.EqualFold(a, op) {
			return true
		}
	}
	return false
}

func namespaceInList(list []string, ns string) bool {
	if len(list) == 0 {
		return true
	}
	for _, n := range list {
		if n == ns {
			return true
		}
	}
	return false
}

func namespaceLabelsMatch(want, got map[string]string) bool {
	if len(want) == 0 {
		return true
	}
	for k, v := range want {
		if got[k] != v {
			return false
		}
	}
	return true
}
