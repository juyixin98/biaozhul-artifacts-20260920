#define EXTRA_HEADER "should-not-expand.h"

/* 宏生成的 include：明确不支持，必须报错 */
#include EXTRA_HEADER

int bad(void);
