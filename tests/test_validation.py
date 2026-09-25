"""输入校验与失败状态测试：零尺寸拒绝、超宽拒绝、范围/规模限制、错误码。"""

import math
import unittest

from packing.bounds import MAX_RECTANGLES, ValidationError, validate_request


def req(sw, rects):
    return {"strip_width": sw,
            "rectangles": [{"width": w, "height": h} for w, h in rects]}


class TestValidation(unittest.TestCase):

    def test_valid_instance_accepted(self):
        inst = validate_request(req(10, [(3, 4), (2, 2)]))
        self.assertEqual(inst.n, 2)
        self.assertEqual(inst.strip_width, 10.0)

    # ---- 零尺寸必须被拒绝（验收点）----
    def test_zero_width_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            validate_request(req(10, [(0, 5)]))
        self.assertEqual(cm.exception.code, "bad_rectangle_size")

    def test_zero_height_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            validate_request(req(10, [(5, 0)]))
        self.assertEqual(cm.exception.code, "bad_rectangle_size")

    def test_tiny_size_below_tolerance_rejected(self):
        # 1e-12 小于容差 1e-9，按零尺寸拒绝
        with self.assertRaises(ValidationError) as cm:
            validate_request(req(10, [(1e-12, 5)]))
        self.assertEqual(cm.exception.code, "bad_rectangle_size")

    def test_negative_size_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            validate_request(req(10, [(-3, 5)]))
        self.assertEqual(cm.exception.code, "bad_rectangle_size")

    def test_zero_strip_width_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            validate_request(req(0, [(1, 1)]))
        self.assertEqual(cm.exception.code, "bad_strip_width")

    def test_negative_strip_width_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            validate_request(req(-10, [(1, 1)]))
        self.assertEqual(cm.exception.code, "bad_strip_width")

    # ---- NaN / Inf 拒绝 ----
    def test_nan_rejected(self):
        with self.assertRaises(ValidationError):
            validate_request(req(10, [(math.nan, 1)]))
        with self.assertRaises(ValidationError):
            validate_request(req(10, [(1, math.inf)]))

    # ---- 超宽拒绝（验收点）----
    def test_too_wide_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            validate_request(req(10, [(10.0000001, 3)]))
        self.assertEqual(cm.exception.code, "rectangle_too_wide")

    def test_width_equal_strip_accepted(self):
        # 恰好等宽：可以放，允许
        inst = validate_request(req(10, [(10, 3)]))
        self.assertEqual(inst.n, 1)

    def test_width_over_by_less_than_eps_accepted(self):
        # 仅超出 1e-12（< EPS），视为浮点噪声允许
        inst = validate_request(req(10, [(10 + 1e-12, 3)]))
        self.assertEqual(inst.n, 1)

    # ---- 其他失败状态 ----
    def test_empty_rectangles_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            validate_request({"strip_width": 10, "rectangles": []})
        self.assertEqual(cm.exception.code, "empty_rectangles")

    def test_missing_field_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            validate_request({"rectangles": []})
        self.assertEqual(cm.exception.code, "missing_field")

    def test_wrong_type_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            validate_request({"strip_width": "10", "rectangles": []})
        self.assertEqual(cm.exception.code, "bad_strip_width")

    def test_too_many_rectangles_rejected(self):
        r = req(10, [(1, 1)] * (MAX_RECTANGLES + 1))
        with self.assertRaises(ValidationError) as cm:
            validate_request(r)
        self.assertEqual(cm.exception.code, "too_many_rectangles")

    def test_bool_not_accepted_as_number(self):
        with self.assertRaises(ValidationError):
            validate_request(req(True, [(1, 1)]))


if __name__ == "__main__":
    unittest.main()
