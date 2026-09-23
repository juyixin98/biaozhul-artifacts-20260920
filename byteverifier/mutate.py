"""单字节变异工具。

对已编码模块做“恰好一个字节”的扰动，用于验证字节码验证器的健壮性：
任何变异模块都只能被 解码失败 / 验证拒绝 / 运行期资源(或除零)错误 拦下，
**绝不允许在解释器中发生操作数栈下溢**（否则说明验证器有漏洞）。

提供两种变异：

* :func:`flip_bit` / :func:`replace_byte` —— 指定位置的确定性变异（字节翻转
  覆盖整个模块；单字节替换聚焦代码段，尤其保证能构造出越界跳转）。
* :func:`all_single_bit_flips` —— 穷举小模块每个字节的 8 个单 bit 翻转。
"""

from __future__ import annotations

from . import bytecode as bc


def flip_bit(data: bytes, byte_index: int, bit: int) -> bytes:
    """翻转 ``data[byte_index]`` 的第 ``bit`` 位（0..7）。只动一个 bit。"""
    if not (0 <= byte_index < len(data)):
        raise IndexError(f"字节下标 {byte_index} 超出模块长度 {len(data)}")
    if not (0 <= bit <= 7):
        raise ValueError("bit 必须在 0..7")
    b = data[byte_index] ^ (1 << bit)
    return data[:byte_index] + bytes([b]) + data[byte_index + 1:]


def replace_byte(data: bytes, byte_index: int, value: int) -> bytes:
    """把 ``data[byte_index]`` 整个替换为 ``value``（恰好改动一个字节）。"""
    if not (0 <= byte_index < len(data)):
        raise IndexError(f"字节下标 {byte_index} 超出模块长度 {len(data)}")
    if not (0 <= value <= 255):
        raise ValueError("value 必须在 0..255")
    return data[:byte_index] + bytes([value]) + data[byte_index + 1:]


def all_single_bit_flips(data: bytes, cap: int | None = None):
    """生成 (byte_index, bit, mutated_bytes) 的穷举序列。"""
    count = 0
    for i in range(len(data)):
        for bit in range(8):
            yield i, bit, flip_bit(data, i, bit)
            count += 1
            if cap is not None and count >= cap:
                return


def function_regions(module_blob: bytes) -> list[tuple[str, int, int]]:
    """解析模块头部，返回每个函数的 (name, code_start, code_end)。

    变异对象本身可能已损坏，因此这里只读“原模块”。
    """
    regions: list[tuple[str, int, int]] = []
    try:
        i = 4
        count = module_blob[i]; i += 1
        for _ in range(count):
            namelen = module_blob[i]; i += 1
            name = module_blob[i:i + namelen].decode("utf-8", "replace")
            i += namelen
            i += 1                       # ret tag
            nparams = module_blob[i]; i += 1
            i += nparams
            nlocals = module_blob[i]; i += 1
            i += nlocals
            i += 1                       # max stack
            code_len = int.from_bytes(module_blob[i:i + 2], "little"); i += 2
            regions.append((name, i, i + code_len))
            i += code_len
            const_len = int.from_bytes(module_blob[i:i + 2], "little"); i += 2
            for _c in range(const_len):
                tag = module_blob[i]; i += 1
                i += 8 if tag == 1 else 1
    except (IndexError, ValueError):
        pass
    return regions


def code_region(module_blob: bytes) -> tuple[int, int]:
    """尽力返回第一个函数代码段 (start, end_exclusive) 的字节范围。

    头部布局固定：4 magic + 1 count + 1 namelen + name + 1 ret +
    1 nparams + params + 1 nlocals + locals + 1 maxstack，随后 2B code_len。
    变异对象本身可能已损坏，因此这里只读“原模块”。
    """
    regions = function_regions(module_blob)
    if regions:
        _, s, e = regions[0]
        return s, e
    return (0, len(module_blob))


def jump_operand_mutation(module_blob: bytes, ins_pc: int,
                          new_target: int) -> bytes:
    """构造“恰好改动跳转目标”的变异（通常 1 个字节，跨越 256 边界时为 2）。

    ``ins_pc`` 是跳转指令相对其函数代码段起点的偏移。
    """
    start, _ = code_region(module_blob)
    lo = start + ins_pc + 1
    old = int.from_bytes(module_blob[lo:lo + 2], "little")
    new = new_target & 0xFFFF
    changed = (
        (old & 0xFF) != (new & 0xFF)
        or (old >> 8) != (new >> 8)
    )
    n_changed = int((old & 0xFF) != (new & 0xFF)) + int((old >> 8) != (new >> 8))
    blob = (
        module_blob[:lo]
        + new.to_bytes(2, "little")
        + module_blob[lo + 2:]
    )
    return blob
