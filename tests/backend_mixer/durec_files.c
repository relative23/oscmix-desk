/* DURec file list reports through the actual handleregs (patch 0020).
 * The UCX II reports the list in signed 16-bit registers. Before 0020 a count of
 * zero after an index, a negative count or a negative index other than -1 ended
 * the backend through fatal(NULL), which printed "Invalid argument" whatever had
 * failed. Each such report is replayed here, and so are reports the backend
 * survived before, which must keep their old effect: built with -DSURVIVING_ONLY
 * against the series before 0020 those pass unchanged (tests/series_carry.py).
 * Only the OSC sink and realloc are substituted. */
#define _XOPEN_SOURCE 700  /* as oscmix.c, which these headers precede */
#include <errno.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>
static long fail_after = -1;  /* reallocs that succeed before one fails; -1 never */
static void *probe_realloc(void *ptr, size_t len)
{
    if (fail_after == 0) {
        errno = ENOMEM;
        return NULL;
    }
    if (fail_after > 0)
        --fail_after;
    return realloc(ptr, len);
}
#define realloc probe_realloc
#ifndef BACKEND_SOURCE
#define BACKEND_SOURCE "oscmix.c"
#endif
#include BACKEND_SOURCE
#undef realloc
extern const struct device ffucxii;
static struct input input_store[40];
static struct output output_store[20];
static float matrix[20][40];
static unsigned char observed[20][20];
static int counts[16], ncounts, cases;
static uint32_t be32(const unsigned char *p)
{
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}
/* Collect every /durec/numfiles value of one bundle. */
void writeosc(const void *ptr, size_t len)
{
    const unsigned char *p = ptr, *end = p + len;
    assert(len >= 16 && memcmp(p, "#bundle", 8) == 0);
    for (p += 16; p < end;) {
        uint32_t size = be32(p);
        const unsigned char *msg = p + 4;
        assert(msg + size <= end);
        if (strcmp((const char *)msg, "/durec/numfiles") == 0) {
            const unsigned char *type = msg + 16;  /* "/durec/numfiles\0" is 16 bytes */
            assert(strcmp((const char *)type, ",i") == 0 && ncounts < 16);
            counts[ncounts++] = (int)be32(type + 4);
        }
        p = msg + size;
    }
}
void writemidi(const void *ptr, size_t len) { (void)ptr; (void)len; }
void oscsource(unsigned value) { (void)value; }
static void initialize(void)
{
    static bool mapped;
    if (!mapped) {
        memset(nodeindex, 0xFF, sizeof nodeindex);
        maptree(roottree, 0);
        mapped = true;
    }
    inputs = input_store; outputs = output_store; device = &ffucxii;
    for (int o = 0; o < 20; o++) { outputs[o].mix = matrix[o]; outputs[o].mix_observed = observed[o]; }
    for (int i = 0; i < 40; i++) inputs[i].width = 100;
    free(durec.files);
    durec.files = NULL; durec.fileslen = 0; durec.index = -1;
    ncounts = 0; fail_after = -1;
}
static void report(unsigned reg, unsigned value)
{
    uint_least32_t word = (uint_least32_t)reg << 16 | value;
    handleregs(&word, 1);
    oscflush();
}
enum { NUMFILES = 0x3586, INDEX = 0x358A, NAME0 = 0x358B };
static void expect(size_t fileslen, int index, int n, const int *sent)
{
    assert(durec.fileslen == fileslen && durec.index == index);
    assert((fileslen == 0) == (durec.files == NULL));
    assert(ncounts == n);
    for (int i = 0; i < n; i++) assert(counts[i] == sent[i]);
    ncounts = 0;
    ++cases;
}
/* What ``body`` writes to stderr in a child, which must exit with ``code``. */
static void child_stderr(void (*body)(void), int code, const char *want)
{
    int fds[2];
    char got[256] = {0};
    assert(pipe(fds) == 0);
    fflush(stdout); fflush(stderr);
    pid_t pid = fork();
    assert(pid >= 0);
    if (pid == 0) {
        close(fds[0]); dup2(fds[1], 2);
        body();
        _exit(0);
    }
    close(fds[1]);
    /* Read to the end: stderr is unbuffered, so one message arrives in several writes. */
    size_t used = 0;
    ssize_t n;
    while (used < sizeof got - 1 && (n = read(fds[0], got + used, sizeof got - 1 - used)) > 0)
        used += (size_t)n;
    close(fds[0]);
    int status;
    assert(waitpid(pid, &status, 0) == pid && WIFEXITED(status) && WEXITSTATUS(status) == code);
    assert(strcmp(got, want) == 0);
    ++cases;
}
static void ending_reports(void)
{
    /* An index, then no files: the list empties instead of ending the backend. */
    initialize();
    report(INDEX, 3); expect(4, 3, 1, (const int[]){4});
    report(NUMFILES, 0); expect(0, -1, 1, (const int[]){0});
    report(NAME0, 0x4142); expect(0, -1, 0, NULL);
    /* A negative index selects no file, -1 as before and -32767 now as well. */
    initialize();
    report(INDEX, 0xFFFF); expect(0, -1, 0, NULL);
    report(INDEX, 0x8001); expect(0, -1, 0, NULL);
    /* A negative count names no list and leaves the one there. */
    initialize();
    report(NUMFILES, 5); expect(5, -1, 1, (const int[]){5});
    report(NUMFILES, 0x8000); expect(5, -1, 0, NULL);
}
static void surviving_reports(void)
{
    /* What the backend did before 0020 and still does. */
    initialize();
    report(NUMFILES, 5); expect(5, -1, 1, (const int[]){5});
    report(NUMFILES, 5); expect(5, -1, 0, NULL);
    report(INDEX, 2); expect(5, 2, 0, NULL);
    report(NAME0, 0x4142);
    assert(memcmp(durec.files[2].name, "BA", 2) == 0);
    expect(5, 2, 0, NULL);
    report(INDEX, 7); expect(8, 7, 1, (const int[]){8});
    report(NUMFILES, 3); expect(3, -1, 1, (const int[]){3});
}
static void failing_growth(void)
{
    initialize();
    report(NUMFILES, 5);
    struct durecfile *kept = durec.files;
    ncounts = 0;
    fail_after = 0;
    report(NUMFILES, 9);
    assert(durec.files == kept && durec.fileslen == 5 && ncounts == 0);
    report(INDEX, 12);
    assert(durec.files == kept && durec.fileslen == 5 && durec.index == -1 && ncounts == 0);
}
static void fatal_null(void) { errno = ENOMEM; fatal(NULL); }
static void fatal_colon(void) { errno = EIO; fatal("read %d:", 7); }
int main(void)
{
#ifdef SURVIVING_ONLY
    /* Built against the series before 0020: these reports already had these effects. */
    surviving_reports();
    printf("PASS surviving_durec_cases=%d\n", cases);
    return 0;
#endif
    ending_reports();
    surviving_reports();
    child_stderr(failing_growth, 0,
                 "DURec file list: Cannot allocate memory\nDURec file list: Cannot allocate memory\n");
    child_stderr(fatal_null, 1, "Cannot allocate memory\n");
    child_stderr(fatal_colon, 1, "read 7: Input/output error\n");
    printf("PASS actual_durec_cases=%d\n", cases);
    return 0;
}
