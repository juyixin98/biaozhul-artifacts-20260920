;; 样例 4：内存增长。
;;
;; 输入：8 字节小端 u64 = 每次增长的页数，模块循环 memory.grow，
;; 直到超过计量版本规定的内存上限。宿主 limiter 返回错误，
;; growth 以陷阱终止，执行失败。
(module
  (memory (export "memory") 1)

  (func (export "alloc") (param $size i32) (result i32)
    (i32.const 64))

  (func (export "run") (param $in_ptr i32) (param $in_len i32) (result i64)
    (local $step i32)
    (i32.load (local.get $in_ptr))
    (local.set $step)
    ;; 每页 64KiB。step 为 0 时强制 1 页，保证一定触碰上限。
    (local.get $step)
    (i32.eqz)
    (if (then (i32.const 1) (local.set $step)))
    (loop $grow_loop
      (local.get $step)
      (memory.grow)
      ;; -1 表示 grow 被 limiter 拒绝（本实现 limiter 直接触发陷阱，
      ;; 走到这里说明返回了软失败，显式 abort 使执行不成功）。
      (i32.const -1)
      (i32.eq)
      (if (then (unreachable)))
      (br $grow_loop))
    (i64.const 0))
)
