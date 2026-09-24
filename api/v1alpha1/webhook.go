package v1alpha1

import (
	"context"
	"fmt"
	"reflect"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"
)

// ResourceRequestWebhook validates ResourceRequest objects: units and upper
// bounds on create, and spec immutability on update.
type ResourceRequestWebhook struct{}

// SetupResourceRequestWebhook registers the validating webhook with the manager.
func SetupResourceRequestWebhook(mgr ctrl.Manager) error {
	return ctrl.NewWebhookManagedBy(mgr).
		For(&ResourceRequest{}).
		WithValidator(ResourceRequestWebhook{}).
		Complete()
}

var _ admission.CustomValidator = ResourceRequestWebhook{}

// ValidateCreate enforces units and upper bounds.
func (ResourceRequestWebhook) ValidateCreate(_ context.Context, obj runtime.Object) (admission.Warnings, error) {
	rr, ok := obj.(*ResourceRequest)
	if !ok {
		return nil, fmt.Errorf("expected a ResourceRequest, got %T", obj)
	}
	if errs := ValidateResourceRequestSpec(&rr.Spec); len(errs) > 0 {
		return nil, apierrors.NewInvalid(
			GroupVersion.WithKind("ResourceRequest").GroupKind(), rr.Name, errs)
	}
	return nil, nil
}

// ValidateUpdate makes the spec immutable: a reservation is a one-shot
// contract, changing it after the fact would break ledger accounting.
func (ResourceRequestWebhook) ValidateUpdate(_ context.Context, oldObj, newObj runtime.Object) (admission.Warnings, error) {
	oldRR, ok := oldObj.(*ResourceRequest)
	if !ok {
		return nil, fmt.Errorf("expected a ResourceRequest, got %T", oldObj)
	}
	newRR, ok := newObj.(*ResourceRequest)
	if !ok {
		return nil, fmt.Errorf("expected a ResourceRequest, got %T", newObj)
	}
	if !reflect.DeepEqual(oldRR.Spec, newRR.Spec) {
		return nil, apierrors.NewForbidden(
			GroupVersion.WithResource("resourcerequests").GroupResource(), newRR.Name,
			fmt.Errorf("spec is immutable; delete the request and create a new one instead"))
	}
	if errs := ValidateResourceRequestSpec(&newRR.Spec); len(errs) > 0 {
		return nil, apierrors.NewInvalid(
			GroupVersion.WithKind("ResourceRequest").GroupKind(), newRR.Name, errs)
	}
	return nil, nil
}

// ValidateDelete imposes no restrictions.
func (ResourceRequestWebhook) ValidateDelete(_ context.Context, _ runtime.Object) (admission.Warnings, error) {
	return nil, nil
}
