"""Fixed-point IIR offline filtering package.

Pure backend, NumPy only. Modules:

- ``qformat``   : signed Qm.n fixed-point primitives (scaling, rounding, saturation)
- ``biquad``    : float & fixed-point cascaded second-order sections (DF2T)
- ``design``    : float filter design helpers and SOS coefficient containers
- ``stability``: post-quantization stability / limit-cycle risk analysis
- ``analysis``  : impulse / large-signal / frequency-response experiments
- ``signals``   : synthetic signals and PCM file I/O
- ``service``   : request-driven offline processing (JSON in, numbers/files out)
- ``cli``       : command line front-end for the service
"""

from .qformat import QFormat, QuantResult, saturate, to_fixed, to_float
from .biquad import (
    SOS,
    FilterConfig,
    FilterResult,
    filter_float,
    filter_fixed,
)
from .design import butter_lowpass, cheby1_lowpass
from .stability import (
    pole_radius_report,
    simulate_zero_input,
    assess_stability,
    StabilityReport,
)

__all__ = [
    "QFormat",
    "QuantResult",
    "saturate",
    "to_fixed",
    "to_float",
    "SOS",
    "FilterConfig",
    "FilterResult",
    "filter_float",
    "filter_fixed",
    "butter_lowpass",
    "cheby1_lowpass",
    "pole_radius_report",
    "simulate_zero_input",
    "assess_stability",
    "StabilityReport",
]
