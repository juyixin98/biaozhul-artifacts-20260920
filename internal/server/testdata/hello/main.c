#include <stdio.h>

#include "app_config.h"
#include "util.h"

int main(void) {
    printf("app_version=%s greeting=%s\n", APP_VERSION, greeting());
    return 0;
}
