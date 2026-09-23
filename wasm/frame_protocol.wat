;; 协议操作示例：长度前缀分帧 + CRC-32 校验往返（真实计算，非桩）。
;;
;; 输入布局：
;;   [0..4]   小端 u32：帧区字节数 n（0 < n <= 4096）
;;   [4..4+n] 原始帧区：若干个 [u16le payload_len][payload] 串联
;; 行为：
;;   1. 从帧区真实解析每帧 payload（边界检查；截断的帧 -> 返回 2，不提交）
;;   2. 对每帧 payload 真实计算 CRC-32（IEEE 0xEDB88320 反射多项式，
;;      256 项表在来宾内存中运行时真实生成）
;;   3. 输出重新封装的帧流：每帧 [u16le len][payload][u32le crc32]
;;   4. 往返自检：对“已封装帧的 payload 部分”再算一次 CRC，必须等于尾部 CRC，
;;      否则返回 3。成功时写 KV "proto"=<总帧数 u32le>，返回 0。
(module
  (import "gas" "input_read"   (func $input_read   (param i32 i32) (result i32)))
  (import "gas" "output_write" (func $output_write (param i32 i32) (result i32)))
  (import "gas" "kv_put"       (func $kv_put       (param i32 i32 i32) (result i32)))

  (memory (export "memory") 1)

  ;; 内存布局
  ;;  8192  CRC-32 表（256*4 = 1024 字节）
  ;; 12288  输入（4 字节头 + 最多 4096 帧区 = 4100 字节，终于 16388）
  ;; 20000  帧数
  ;; 20064  键 "proto\0"
  ;; 24576  输出封装帧（最坏 2048 帧 * 8 字节 = 16384，终于 40960）

  ;; ===== CRC-32 表生成：运行时真实生成 256 项 =====
  (func $gen_crc_table
    (local $i i32) (local $j i32) (local $c i32)
    (local.set $i (i32.const 0))
    (block $done (loop $li
      (br_if $done (i32.ge_u (local.get $i) (i32.const 256)))
      (local.set $c (local.get $i))
      (local.set $j (i32.const 0))
      (block $done2 (loop $lj
        (br_if $done2 (i32.ge_u (local.get $j) (i32.const 8)))
        ;; c = (c & 1) ? (c >> 1) ^ 0xEDB88320 : c >> 1
        (if (i32.and (local.get $c) (i32.const 1))
          (then (local.set $c
            (i32.xor (i32.shr_u (local.get $c) (i32.const 1))
                    (i32.const 0xEDB88320))))
          (else (local.set $c (i32.shr_u (local.get $c) (i32.const 1)))))
        (local.set $j (i32.add (local.get $j) (i32.const 1)))
        (br $lj)))
      (i32.store
        (i32.add (i32.const 8192) (i32.shl (local.get $i) (i32.const 2)))
        (local.get $c))
      (local.set $i (i32.add (local.get $i) (i32.const 1)))
      (br $li))))

  ;; $crc32(ptr,len) -> i32：对内存 [ptr,ptr+len) 计算 CRC-32
  (func $crc32 (param $ptr i32) (param $len i32) (result i32)
    (local $i i32) (local $crc i32) (local $b i32) (local $idx i32)
    (local.set $crc (i32.const 0xFFFFFFFF))
    (local.set $i (i32.const 0))
    (block $done (loop $l
      (br_if $done (i32.ge_u (local.get $i) (local.get $len)))
      (local.set $b (i32.load8_u
        (i32.add (local.get $ptr) (local.get $i))))
      ;; idx = (crc ^ b) & 0xFF
      (local.set $idx
        (i32.and (i32.xor (local.get $crc) (local.get $b)) (i32.const 255)))
      ;; crc = (crc >> 8) ^ table[idx]
      (local.set $crc
        (i32.xor (i32.shr_u (local.get $crc) (i32.const 8))
          (i32.load
            (i32.add (i32.const 8192)
                     (i32.shl (local.get $idx) (i32.const 2))))))
      (local.set $i (i32.add (local.get $i) (i32.const 1)))
      (br $l)))
    (i32.xor (local.get $crc) (i32.const 0xFFFFFFFF)))

  (func (export "run") (result i32)
    (local $n i32)        ;; 帧区总字节数
    (local $total i32)    ;; input_read 实际返回
    (local $frames i32)   ;; 解析出的帧数
    (local $pos i32)      ;; 帧区内游标
    (local $plen i32)
    (local $pptr i32)
    (local $crc i32)
    (local $optr i32)     ;; 输出游标（相对输出基址 24576）
    (local $crc2 i32)
    (local $k i32)
    (local $payload_out i32)

    (call $gen_crc_table)

    (local.set $total (call $input_read (i32.const 12288) (i32.const 4100)))
    (if (i32.lt_u (local.get $total) (i32.const 4))
      (then (return (i32.const 2))))
    (local.set $n (i32.load (i32.const 12288)))
    (if (i32.or (i32.eqz (local.get $n))
                (i32.gt_u (local.get $n) (i32.const 4096)))
      (then (return (i32.const 2))))

    ;; 帧区基址 12292
    (local.set $pos (i32.const 0))
    (local.set $optr (i32.const 0))

    (block $done (loop $parse
      (br_if $done (i32.ge_u (local.get $pos) (local.get $n)))
      (if (i32.gt_u (i32.add (local.get $pos) (i32.const 2)) (local.get $n))
        (then (return (i32.const 2))))
      (local.set $plen (i32.load16_u
        (i32.add (i32.const 12292) (local.get $pos))))
      (local.set $pos (i32.add (local.get $pos) (i32.const 2)))
      (if (i32.gt_u (i32.add (local.get $pos) (local.get $plen))
                    (local.get $n))
        (then (return (i32.const 2))))
      (local.set $pptr (i32.add (i32.const 12292) (local.get $pos)))
      (local.set $crc (call $crc32 (local.get $pptr) (local.get $plen)))

      ;; 封装：[u16 len][payload][u32 crc]
      (i32.store16
        (i32.add (i32.const 24576) (local.get $optr))
        (local.get $plen))
      (local.set $optr (i32.add (local.get $optr) (i32.const 2)))
      ;; 逐字节复制 payload
      (local.set $k (i32.const 0))
      (block $cp (loop $cpl
        (br_if $cp (i32.ge_u (local.get $k) (local.get $plen)))
        (i32.store8
          (i32.add (i32.const 24576)
                   (i32.add (local.get $optr) (local.get $k)))
          (i32.load8_u (i32.add (local.get $pptr) (local.get $k))))
        (local.set $k (i32.add (local.get $k) (i32.const 1)))
        (br $cpl)))
      ;; payload 在输出区中的位置（此刻 optr 已跳过 2 字节 len）
      (local.set $payload_out
        (i32.add (i32.const 24576) (local.get $optr)))
      (local.set $optr (i32.add (local.get $optr) (local.get $plen)))
      (i32.store
        (i32.add (i32.const 24576) (local.get $optr))
        (local.get $crc))
      (local.set $optr (i32.add (local.get $optr) (i32.const 4)))

      ;; 往返自检：对已封装帧的 payload 再算一次 CRC
      (local.set $crc2
        (call $crc32 (local.get $payload_out) (local.get $plen)))
      (if (i32.ne (local.get $crc2) (local.get $crc))
        (then (return (i32.const 3))))

      (local.set $pos (i32.add (local.get $pos) (local.get $plen)))
      (local.set $frames (i32.add (local.get $frames) (i32.const 1)))
      (br $parse)))

    ;; 输出封装帧流
    (drop (call $output_write (i32.const 24576) (local.get $optr)))

    ;; 提交帧数，键 "proto"
    (i32.store (i32.const 20000) (local.get $frames))
    (i32.store8 (i32.const 20064) (i32.const 0x70))  ;; p
    (i32.store8 (i32.const 20065) (i32.const 0x72))  ;; r
    (i32.store8 (i32.const 20066) (i32.const 0x6F))  ;; o
    (i32.store8 (i32.const 20067) (i32.const 0x74))  ;; t
    (i32.store8 (i32.const 20068) (i32.const 0x6F))  ;; o
    (i32.store8 (i32.const 20069) (i32.const 0x00))
    (drop (call $kv_put (i32.const 20064) (i32.const 20000) (i32.const 4)))
    (i32.const 0))
)
