/*
 * libxdgopen_shim.so —— APK 内置的假 xdg-open。
 *
 * kdocs-cli auth login 在打印授权 URL 后会调用 xdg-open；Android App 里没有
 * 这个命令，proot 会把本文件 bind 到 /usr/bin/xdg-open。实现只做一件事：
 * 把 argv[1] 追加写入 YIKOU_AUTH_URL_FILE，AuthCoordinator 轮询该文件后用
 * Custom Tabs 拉起浏览器。
 *
 * 不产生任何输出，避免把 OAuth URL / code 混进 kdocs-cli 的 stdout JSON；
 * 退出码 0 让 CLI 继续等待回调。
 */
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv) {
    if (argc < 2 || argv[1] == NULL || argv[1][0] == '\0') {
        return 1;
    }
    const char *path = getenv("YIKOU_AUTH_URL_FILE");
    if (path == NULL || path[0] == '\0') {
        return 1;
    }
    FILE *fp = fopen(path, "a");
    if (fp == NULL) {
        return 1;
    }
    if (fputs(argv[1], fp) == EOF || fputc('\n', fp) == EOF) {
        fclose(fp);
        return 1;
    }
    if (fclose(fp) != 0) {
        return 1;
    }
    return 0;
}
