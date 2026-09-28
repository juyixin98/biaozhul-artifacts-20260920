"""Small integer matrix multiply-accumulate inference engine.

Modules:
    tensor_types - quantized tensor representation and invariants
    quantize     - calibration and (de)quantization math (frozen parameters)
    kernels      - explicit integer MAC kernel + requantization
    graph        - computation graph (quantize / linear / relu / dequantize)
    model        - trained float state, quantized model artifacts, versioning
    numerics     - independent error metrics and analytic error bounds
    errors       - typed errors with ACCEPT / REJECT / INDETERMINATE categories
"""

QUANTIZER_VERSION = "quant-asy-v1.0.0"
"""Version of quantization semantics.

Bumped whenever rounding, zero-point or scale semantics change. Artifacts
carrying a different version are refused to load - calibration parameters are
bound to the semantics that produced them.
"""

__all__ = ["QUANTIZER_VERSION"]
