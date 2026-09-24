package integration

import (
	"fmt"
	"os"

	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
	clientcmdapi "k8s.io/client-go/tools/clientcmd/api"
)

// writeKubeconfig writes a standalone kubeconfig for the envtest apiserver so
// the storage-migrate binary can run against the test cluster without being
// linked into the test process.
func writeKubeconfig(path string, cfg *rest.Config) error {
	caFile := path + "-ca.crt"
	if err := os.WriteFile(caFile, cfg.TLSClientConfig.CAData, 0o600); err != nil {
		return fmt.Errorf("write CA: %w", err)
	}

	cluster := clientcmdapi.NewCluster()
	cluster.Server = cfg.Host
	cluster.CertificateAuthority = caFile

	auth := clientcmdapi.NewAuthInfo()
	if cfg.KeyData != nil {
		keyFile := path + "-client.key"
		certFile := path + "-client.crt"
		if err := os.WriteFile(keyFile, cfg.TLSClientConfig.KeyData, 0o600); err != nil {
			return err
		}
		if err := os.WriteFile(certFile, cfg.TLSClientConfig.CertData, 0o600); err != nil {
			return err
		}
		auth.ClientCertificate = certFile
		auth.ClientKey = keyFile
	}
	if cfg.BearerToken != "" {
		auth.Token = cfg.BearerToken
	}
	if u := cfg.Username; u != "" {
		auth.Username = u
		auth.Password = cfg.Password
	}

	ctxCfg := clientcmdapi.NewContext()
	ctxCfg.Cluster = "envtest"
	ctxCfg.AuthInfo = "envtest-user"
	ctxCfg.Namespace = "itest"

	kc := clientcmdapi.NewConfig()
	kc.Clusters["envtest"] = cluster
	kc.AuthInfos["envtest-user"] = auth
	kc.Contexts["envtest"] = ctxCfg
	kc.CurrentContext = "envtest"

	return clientcmd.WriteToFile(*kc, path)
}
