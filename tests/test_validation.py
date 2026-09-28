import pytest

from krylov_expm.config import SolverConfig
from krylov_expm.errors import ExpmvFailure, FailureCategory
from krylov_expm.models import CooMatrix, ExpmvRequest
from krylov_expm.validation import validate_and_normalize

CONFIG = SolverConfig()


def make_request(**overrides):
    payload = {
        "matrix": CooMatrix(shape=[2, 2], row=[0, 1], col=[0, 1], data=[1.0, -1.0]),
        "vector": [1.0, 2.0],
        "t": 0.5,
    }
    payload.update(overrides)
    return ExpmvRequest(**payload)


def test_zero_time_is_flagged_trivial():
    result = validate_and_normalize(make_request(t=0.0), CONFIG)
    assert result.trivial_case == "zero_time"


def test_zero_vector_is_flagged_trivial():
    result = validate_and_normalize(make_request(vector=[0.0, 0.0]), CONFIG)
    assert result.trivial_case == "zero_vector"


def test_negative_time_is_accepted_as_backward_propagation():
    result = validate_and_normalize(make_request(t=-1.25), CONFIG)
    assert result.t == -1.25
    assert result.trivial_case is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"matrix": CooMatrix(shape=[2, 3], row=[0], col=[0], data=[1.0])},
        {"matrix": CooMatrix(shape=[2, 2], row=[0], col=[0, 1], data=[1.0])},
        {"matrix": CooMatrix(shape=[2, 2], row=[0, 5], col=[0, 1], data=[1.0, 2.0])},
        {"matrix": CooMatrix(shape=[2, 2], row=[0], col=[0], data=[float("nan")])},
        {"vector": [1.0]},
        {"vector": [1.0, float("inf")]},
        {"t": float("nan")},
        {"tol": 0.0},
        {"tol": 1.5},
    ],
)
def test_invalid_inputs_raise_validation_error(overrides):
    with pytest.raises(ExpmvFailure) as excinfo:
        validate_and_normalize(make_request(**overrides), CONFIG)
    assert excinfo.value.category is FailureCategory.VALIDATION_ERROR
