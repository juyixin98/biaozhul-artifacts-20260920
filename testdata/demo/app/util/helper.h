#ifndef UTIL_HELPER_H
#define UTIL_HELPER_H

/* 嵌套路径：util/helper.h -> util/detail/format.h */
#include "detail/format.h"
/* 同名头对照：相对包含者目录 util/ 下没有 net/name.h，
   引号 include 继续落到系统目录，命中 sysinc/net/name.h，
   与 app/name.h 是两个不同节点。 */
#include "net/name.h"

void helper(void);

#endif
