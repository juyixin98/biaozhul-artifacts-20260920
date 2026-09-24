package controller

import (
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"

	snapshotv1alpha1 "snapshotcontroller/api/v1alpha1"
	"snapshotcontroller/internal/worker"
)

// JobImageConfig controls the worker container image. The default is a kind
// node image so that a stock kind cluster needs no image build/pull for local
// E2E runs (it already contains kubectl, bash and coreutils).
type JobImageConfig struct {
	Image           string
	ImagePullPolicy corev1.PullPolicy
	ServiceAccount  string
	BackoffLimit    int32
}

const (
	// annotationSnapshotUID carries the Snapshot UID into the Pod so the
	// worker can set a correct ownerReference on its result object via the
	// downward API (the Pod's own ownerReference points at the Job, not the
	// Snapshot).
	annotationSnapshotUID = "snapshot.example.com/snapshot-uid"
)

// buildWorkerJob creates the (not yet persisted) worker Job for a generation.
//
// The Job mounts the source ConfigMap at /data and runs embedded worker.sh
// with generation metadata passed through env vars. Ownership makes Kubernetes
// garbage collection a safety net; labels are what the reconciler filters on.
func buildWorkerJob(s *snapshotv1alpha1.Snapshot, cfg JobImageConfig) *batchv1.Job {
	name := generationName(s.Name, s.Generation)
	return &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Name:            name,
			Namespace:       s.Namespace,
			Labels:          standardLabels(s.Name, s.Generation),
			OwnerReferences: []metav1.OwnerReference{ownerRef(s)},
		},
		Spec: batchv1.JobSpec{
			BackoffLimit: ptr.To(cfg.BackoffLimit),
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels: standardLabels(s.Name, s.Generation),
					Annotations: map[string]string{
						annotationSnapshotUID: string(s.UID),
					},
				},
				Spec: corev1.PodSpec{
					RestartPolicy:      corev1.RestartPolicyNever,
					ServiceAccountName: cfg.ServiceAccount,
					Containers: []corev1.Container{{
						Name:            "snapshot-worker",
						Image:           cfg.Image,
						ImagePullPolicy: cfg.ImagePullPolicy,
						Command:         []string{"bash", "-c", worker.Script},
						Env: []corev1.EnvVar{
							{Name: "SNAPSHOT_NAME", Value: s.Name},
							{Name: "SNAPSHOT_NAMESPACE", Value: s.Namespace},
							{Name: "SNAPSHOT_GENERATION", Value: generationLabel(s)},
							{Name: "SOURCE_CONFIGMAP", Value: s.Spec.SourceConfigMap},
							{Name: "SUB_PATH", Value: s.Spec.SubPath},
							{Name: "RESULT_CONFIGMAP", Value: name},
							{
								Name: "SNAPSHOT_UID",
								ValueFrom: &corev1.EnvVarSource{
									FieldRef: &corev1.ObjectFieldSelector{
										FieldPath: "metadata.annotations['" + annotationSnapshotUID + "']",
									},
								},
							},
						},
						VolumeMounts: []corev1.VolumeMount{{
							Name:      "source",
							MountPath: "/data",
							ReadOnly:  true,
						}},
					}},
					Volumes: []corev1.Volume{{
						Name: "source",
						VolumeSource: corev1.VolumeSource{
							ConfigMap: &corev1.ConfigMapVolumeSource{
								LocalObjectReference: corev1.LocalObjectReference{Name: s.Spec.SourceConfigMap},
							},
						},
					}},
				},
			},
		},
	}
}

func ownerRef(s *snapshotv1alpha1.Snapshot) metav1.OwnerReference {
	return metav1.OwnerReference{
		APIVersion:         snapshotv1alpha1.GroupVersion.String(),
		Kind:               "Snapshot",
		Name:               s.Name,
		UID:                s.UID,
		Controller:         ptr.To(true),
		BlockOwnerDeletion: ptr.To(true),
	}
}

func namespacedName(s *snapshotv1alpha1.Snapshot) types.NamespacedName {
	return types.NamespacedName{Namespace: s.Namespace, Name: generationName(s.Name, s.Generation)}
}
