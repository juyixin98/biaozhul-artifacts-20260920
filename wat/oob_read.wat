;; 样例 5：越界访问。
;;
;; run 从线性内存第 64KiB 页之后读取（初始仅 1 页）。
;; memory.size 是页数；size*65536 恰为当前有效地址上界，
;; 在该地址读取立即触发越界陷阱。失败执行不产生任何提交。
(module
  (memory (export "memory") 1)

  (func (export "alloc") (param $size i32) (result i32)
    (i32.const 64))

  (func (export "run") (param $in_ptr i32) (param $in_len i32) (result i64)
    (drop (i32.load
      (i32.shl (memory.size) (i32.const 16))))
    (i64.const 0))
)
