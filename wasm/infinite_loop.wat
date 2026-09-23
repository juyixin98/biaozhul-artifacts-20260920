;; 无限循环示例
;;
;; 行为：进入无条件死循环，只做空转与计数，不产生任何写入/输出。
;; 预期：Wasmtime fuel 耗尽 -> out_of_fuel，执行终止，宿主状态不变。
(module
  (memory (export "memory") 1)

  (func (export "run") (result i32)
    (local $i i32)
    (loop $forever
      (local.set $i (i32.add (local.get $i) (i32.const 1)))
      ;; 把计数写回内存，防止编译器把循环整体优化掉。
      (i32.store (i32.const 0) (local.get $i))
      (br $forever))
    (i32.const 0)))
