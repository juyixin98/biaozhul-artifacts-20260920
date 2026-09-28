"""hvpsvc - Hessian-vector product service for restricted differentiable expressions.

The package never materializes a full Hessian. Gradients are obtained by
reverse-mode AD which *builds a new computation graph* for the gradient;
Hessian-vector products are obtained by forward-mode (tangent) propagation
through that gradient graph (forward-over-reverse).
"""

__version__ = "0.1.0"
