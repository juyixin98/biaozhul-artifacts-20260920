from app.fixedpoint import WAD, fmt_wad, parse_wad, wdiv, wmul


def test_wmul_wdiv_roundtrip():
    assert wmul(WAD, WAD) == WAD
    assert wdiv(WAD, WAD) == WAD
    # 3 * 0.5 = 1.5
    assert wmul(3 * WAD, WAD // 2) == 15 * 10**17
    # wdiv 给出 WAD 缩放的比率 a/b: (1 WAD)/3 = (1/3) 的 WAD 表示 = WAD^2/3
    assert wdiv(WAD, 3) == WAD * WAD // 3
    # 原始整数相除并按 WAD 缩放: 1/2 -> 0.5 WAD
    assert wdiv(1, 2) == WAD // 2


def test_wmul_floor():
    # 2/3 * 1 应为 floor(2/3 WAD)
    assert wmul(2 * WAD // 3, WAD) == 2 * WAD // 3
    # wmul 向下取整: 0.000...001 * 0.000...002 < 1 wei
    assert wmul(1, 2) == 0


def test_parse_wad_decimal_exact():
    assert parse_wad("1") == WAD
    assert parse_wad("0.5") == WAD // 2
    assert parse_wad("12.5") == 25 * WAD // 2
    assert parse_wad(10) == 10 * WAD
    # 超过 18 位小数向下截断, 绝不用 float
    assert parse_wad("0.0000000000000000019") == 1


def test_fmt_wad():
    assert fmt_wad(WAD) == "1"
    assert fmt_wad(3 * WAD // 2) == "1.5"
    assert fmt_wad(0) == "0"
