//go:build e2e

package e2e_test

import (
	"context"
	"fmt"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/wait"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func controllerDeployment(e *env) *appsv1.Deployment {
	d := &appsv1.Deployment{}
	if err := e.c.Get(context.Background(), types.NamespacedName{
		Namespace: controllerNamespace, Name: controllerDeploy,
	}, d); err != nil {
		return nil
	}
	return d
}

func scaleController(e *env, replicas int32) error {
	d := controllerDeployment(e)
	if d == nil {
		return fmt.Errorf("controller deployment not found")
	}
	patch := client.MergeFrom(d.DeepCopy())
	d.Spec.Replicas = &replicas
	return e.c.Patch(context.Background(), d, patch)
}

func controllerReplicas(e *env) int32 {
	d := controllerDeployment(e)
	if d == nil {
		return -1
	}
	return d.Status.ReadyReplicas
}

func waitControllerReady(e *env, timeout time.Duration) error {
	return wait.PollUntilContextTimeout(context.Background(), 2*time.Second, timeout, true, func(ctx context.Context) (bool, error) {
		d := &appsv1.Deployment{}
		if err := e.c.Get(ctx, types.NamespacedName{
			Namespace: controllerNamespace, Name: controllerDeploy,
		}, d); err != nil {
			return false, err
		}
		return d.Status.ReadyReplicas >= 1, nil
	})
}

var _ = testing.Verbose
var _ corev1.Pod
