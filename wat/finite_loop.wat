;; 样例 1：有限循环。
;;
;; 输入：8 字节小端 u64 = n。执行 1..=n 累加（n 较小时正常结束）。
;; 过程中：
;;   kv_put("sum", 十进制和)            —— 宿主事务缓冲写入
;;   sha256(十进制和) -> 32 字节         —— 真实密码学哈希（由宿主执行）
;;   kv_put("hash", 32 字节摘要)
;;   kv_get("sum") 读回校验
;; 返回：打包 i64 = (数据指针 << 32) | 字节长度。
(module
  (import "env" "kv_put"  (func $kv_put  (param i32 i32 i32 i32)))
  (import "env" "kv_get"  (func $kv_get  (param i32 i32 i32 i32) (result i32)))
  (import "env" "sha256"  (func $sha256 (param i32 i32 i32)))
  (import "env" "abort"   (func $abort   (param i32 i32)))

  (memory (export "memory") 1)
  ;; 简单 bump allocator 堆顶，初值在 64KB 页内。
  (global $heap (mut i32) (i32.const 2048))

  ;; 宿主 ABI 用到的键落在前 2KB 静态区。
  (data (i32.const 0) "sum\00\00")
  (data (i32.const 8) "hash\00\00\00\00")

  (func $align4
    (i32.const 3)
    (global.get $heap)
    (i32.add)
    (i32.const -4)
    (i32.and)
    (global.set $heap))

  (func $alloc (param $size i32) (result i32)
    (call $align4)
    (global.get $heap)
    (global.get $heap)
    (local.get $size)
    (i32.add)
    (global.set $heap))

  ;; 把 u64 转成十进制 ASCII。
  ;; 在 p 起 24 字节缓冲内先反向书写、再正向归位，返回数字字节长度。
  (func $itoa (param $value i64) (param $p i32) (result i32)
    (local $end i32) (local $digits i32) (local $i i32)
    (local.set $end (i32.add (local.get $p) (i32.const 20)))
    (block $d
      (loop $dl
        (i32.store8
          (local.tee $end (i32.sub (local.get $end) (i32.const 1)))
          (i32.add (i32.const 48)
            (i32.wrap_i64 (i64.rem_u (local.get $value) (i64.const 10)))))
        (local.set $value (i64.div_u (local.get $value) (i64.const 10)))
        (br_if $dl (i64.gt_u (local.get $value) (i64.const 0)))))
    (local.set $digits (i32.sub (i32.add (local.get $p) (i32.const 20)) (local.get $end)))
    (block $c
      (loop $cl
        (i32.store8
          (i32.add (local.get $p) (local.get $i))
          (i32.load8_u (i32.add (local.get $end) (local.get $i))))
        (local.tee $i (i32.add (local.get $i) (i32.const 1)))
        (i32.lt_u (local.get $digits))
        (br_if $cl)))
    (local.get $digits))

  (func (export "alloc") (param $size i32) (result i32)
    (call $alloc (local.get $size)))

  (func (export "run") (param $in_ptr i32) (param $in_len i32) (result i64)
    (local $n i64) (local $i i64) (local $sum i64)
    (local $num_p i32) (local $num_len i32)
    (local $hash_p i32) (local $out_len i32)

    (i64.load (local.get $in_ptr))
    (local.set $n)

    ;; sum = 1 + 2 + ... + n（有限循环；n 很大时燃料先耗尽）。
    (i64.const 1)
    (local.set $i)
    (block $sum_done
      (loop $sum_loop
        (local.get $sum)
        (local.get $i)
        (i64.add)
        (local.set $sum)
        (local.get $i)
        (i64.const 1)
        (i64.add)
        (local.tee $i)
        (local.get $n)
        (i64.le_u)
        (br_if $sum_loop)))

    ;; kv_put("sum", itoa(sum))
    (call $alloc (i32.const 24))
    (local.set $num_p)
    (call $itoa (local.get $sum) (local.get $num_p))
    (local.set $num_len)
    (call $kv_put
      (i32.const 0) (i32.const 3)
      (local.get $num_p) (local.get $num_len))

    ;; hash = sha256(十进制和字符串)；kv_put("hash", hash)
    (call $alloc (i32.const 32))
    (local.set $hash_p)
    (call $sha256
      (local.get $num_p) (local.get $num_len) (local.get $hash_p))
    (call $kv_put
      (i32.const 8) (i32.const 4)
      (local.get $hash_p) (i32.const 32))

    ;; kv_get("sum") 读回（缓冲区容量 24 字节，与十进制字符串长度匹配）。
    (call $kv_get
      (i32.const 0) (i32.const 3)
      (local.get $num_p) (i32.const 24))
    (local.set $out_len)
    (local.get $out_len)
    (i32.const -1)
    (i32.eq)
    (if (then (call $abort (i32.const 0) (i32.const 0))))

    ;; 返回打包 i64：(ptr << 32) | len
    (i64.or
      (i64.shl (i64.extend_i32_u (local.get $num_p)) (i64.const 32))
      (i64.extend_i32_u (local.get $out_len))))
)
