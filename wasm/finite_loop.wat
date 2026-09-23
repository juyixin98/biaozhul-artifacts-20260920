;; 有限循环示例
;;
;; 输入：任意字节串（演示用，1 个字节即可）。
;; 行为：
;;   1. 读取输入字节 n（缺省为 0）
;;   2. 循环 i = 1..=n 做真实整数累加（wrapping 求和 + 平方和）
;;   3. gas::kv_put 写两条状态
;;   4. gas::output_write 输出 8 字节小端：[sum, sum_of_squares]
;;   5. 返回 0（成功 => 提交）
(module
  (import "gas" "input_read"   (func $input_read   (param i32 i32) (result i32)))
  (import "gas" "output_write" (func $output_write (param i32 i32) (result i32)))
  (import "gas" "kv_put"       (func $kv_put       (param i32 i32 i32) (result i32)))

  (memory (export "memory") 1)

  (data (i32.const 0)    "n")
  (data (i32.const 16)   "trace")
  (data (i32.const 4096) "ok:finite")

  (func (export "run") (result i32)
    (local $n i32)
    (local $i i32)
    (local $sum i32)
    (local $sq i32)
    (local $read i32)

    ;; 读 1 字节输入到内存 256
    (local.set $read (call $input_read (i32.const 256) (i32.const 1)))
    (if (i32.eqz (local.get $read))
      (then (i32.store8 (i32.const 256) (i32.const 0))))
    (local.set $n (i32.load8_u (i32.const 256)))

    ;; 有限循环：i = 1..=n
    (local.set $i (i32.const 1))
    (block $done
      (loop $cont
        (br_if $done (i32.gt_u (local.get $i) (local.get $n)))
        (local.set $sum
          (i32.add (local.get $sum) (local.get $i)))
        (local.set $sq
          (i32.add (local.get $sq)
            (i32.mul (local.get $i) (local.get $i))))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br $cont)))

    ;; 输出写入内存 512，小端两个 i32
    (i32.store (i32.const 512) (local.get $sum))
    (i32.store (i32.const 516) (local.get $sq))

    ;; 事务写入（成功返回后才发布）
    (drop (call $kv_put (i32.const 0) (i32.const 512) (i32.const 4)))
    (drop (call $kv_put (i32.const 16) (i32.const 516) (i32.const 4)))

    ;; 输出
    (drop (call $output_write (i32.const 512) (i32.const 8)))
    (i32.const 0)))
