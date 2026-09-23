;; 内存增长示例
;;
;; 输入：前 4 字节小端 u32 = 请求增长的页数（1 页 = 64 KiB）。缺省 1 页。
;; 行为：调用 memory.grow；
;;   * 增长在沙箱上限内：写满新区域首尾两页，输出增长后的内存大小（页数），返回 0
;;   * 增长超过沙箱上限：宿主限制器返回 Err，立即陷阱终止（memory_limit_exceeded）
(module
  (import "gas" "input_read"   (func $input_read   (param i32 i32) (result i32)))
  (import "gas" "output_write" (func $output_write (param i32 i32) (result i32)))

  (memory (export "memory") 1)

  (func (export "run") (result i32)
    (local $pages i32)
    (local $old_pages i32)
    (local $total_pages i32)
    (local $read i32)
    (local $touch_offset i32)

    (local.set $read (call $input_read (i32.const 65532) (i32.const 4)))
    (if (i32.lt_u (local.get $read) (i32.const 4))
      (then (i32.store (i32.const 65532) (i32.const 1))))
    (local.set $pages (i32.load (i32.const 65532)))

    (local.set $old_pages (memory.grow (local.get $pages)))
    ;; 若 grow 普通失败返回 -1（模块自身 max），视为模块级中止
    (if (i32.lt_s (local.get $old_pages) (i32.const 0))
      (then (return (i32.const 2))))

    (local.set $total_pages
      (i32.add (local.get $old_pages) (local.get $pages)))

    ;; 真实触碰新内存：在新区域的起始页和最末页各写一个字节，
    ;; 确保增长是实际可用的，而不仅仅是计数。
    (local.set $touch_offset
      (i32.mul (local.get $old_pages) (i32.const 65536)))
    (i32.store8 (local.get $touch_offset) (i32.const 0xAB))
    (local.set $touch_offset
      (i32.sub
        (i32.mul (local.get $total_pages) (i32.const 65536))
        (i32.const 1)))
    (i32.store8 (local.get $touch_offset) (i32.const 0xCD))

    ;; 输出增长后的总页数（小端 i32）
    (i32.store (i32.const 65528) (local.get $total_pages))
    (drop (call $output_write (i32.const 65528) (i32.const 4)))
    (i32.const 0)))
