"""编码器回绕处理测试（手算验收）。"""

import pytest

from diff_odom.encoder import unwrap_count_delta


class TestUnwrapCountDelta:
    def test_plain_forward(self):
        delta, wrapped = unwrap_count_delta(100, 150, 0, 65535)
        assert delta == 50
        assert wrapped is False

    def test_plain_reverse(self):
        delta, wrapped = unwrap_count_delta(150, 100, 0, 65535)
        assert delta == -50
        assert wrapped is False

    def test_forward_wrap_past_max(self):
        # 65530 -> 10，量程 65536：真实增量 65536 - 65520 = 16
        delta, wrapped = unwrap_count_delta(65530, 10, 0, 65535)
        assert delta == 16
        assert wrapped is True

    def test_reverse_wrap_past_min(self):
        # 10 -> 65530：倒车 16 个计数
        delta, wrapped = unwrap_count_delta(10, 65530, 0, 65535)
        assert delta == -16
        assert wrapped is True

    def test_non_zero_min_range(self):
        # 量程 [100, 109]，模 10：108 -> 101 表示 +3
        delta, wrapped = unwrap_count_delta(108, 101, 100, 109)
        assert delta == 3
        assert wrapped is True

    def test_exactly_half_range_not_unwrapped(self):
        # 恰好半个量程时无法判断方向，按原样返回（约定不修正）
        delta, wrapped = unwrap_count_delta(0, 512, 0, 1023)
        assert delta == 512
        assert wrapped is False

    def test_reading_out_of_range_raises(self):
        with pytest.raises(ValueError):
            unwrap_count_delta(0, 70000, 0, 65535)
        with pytest.raises(ValueError):
            unwrap_count_delta(-1, 0, 0, 65535)
