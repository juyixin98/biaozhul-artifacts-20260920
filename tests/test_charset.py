"""字符集区间运算：手工计算的参考结果。"""

from app.kernel.charset import CharSet, partition_alphabet


def test_union_merges_adjacent():
    a = CharSet.of_range(ord("a"), ord("c"))
    b = CharSet.of_range(ord("d"), ord("f"))
    assert a.union(b).intervals == ((ord("a"), ord("f")),)


def test_intersect_partial_overlap():
    a = CharSet.of_range(10, 20)
    b = CharSet.of_range(15, 25)
    assert a.intersect(b).intervals == ((15, 20),)


def test_intersect_disjoint_is_empty():
    a = CharSet.of_char(ord("a"))
    b = CharSet.of_char(ord("b"))
    assert a.intersect(b).is_empty()


def test_complement_of_newline():
    nl = CharSet.of_char(0x0A)
    comp = nl.complement()
    assert comp.intervals == ((0x00, 0x09), (0x0B, 0x10FFFF))
    assert not comp.contains(0x0A)
    assert comp.contains(0x0D)  # \r 是普通字符（换行模式固定 LF）


def test_subtract():
    a = CharSet.of_range(0, 100)
    b = CharSet.of_range(50, 60)
    assert a.subtract(b).intervals == ((0, 49), (61, 100))


def test_partition_cells_cover_alphabet_disjointly():
    cells = partition_alphabet([CharSet.of_range(10, 20), CharSet.of_range(15, 30)])
    # 端点 10,20,15,30 -> 单元 [0,9] [10,14] [15,20] [21,30] [31,10FFFF]
    assert [c.intervals[0] for c in cells] == [
        (0, 9),
        (10, 14),
        (15, 20),
        (21, 30),
        (31, 0x10FFFF),
    ]
