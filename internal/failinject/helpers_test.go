package failinject

import "k8s.io/apimachinery/pkg/runtime"

func runtimeRawExtension(raw []byte) runtime.RawExtension {
	return runtime.RawExtension{Raw: raw}
}
