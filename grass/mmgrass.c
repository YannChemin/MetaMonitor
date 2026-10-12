/*
 * MODULE:    libmmgrass
 * AUTHOR(S): Yann Chemin
 * PURPOSE:   Publish GRASS GIS module progress to MetaMonitor.
 *
 *            Preloaded into GRASS sessions (LD_PRELOAD, set from the GRASS
 *            env file by MetaMonitor), it interposes G_percent(),
 *            G_progress() and G_fatal_error(): every call is forwarded
 *            unchanged to libgrass_gis, and the progress is also written as
 *            a JSON slot into the MetaMonitor spool.
 *
 *            Python modules report progress through gs.percent(), which
 *            runs "g.message -p"; those calls are credited to the parent
 *            script instead of the short-lived g.message process.
 *
 * SPDX-License-Identifier: Unlicense
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

/* A module that finishes faster than this never gets a slot. */
#define FIRST_DELAY 1.0
#define MIN_INTERVAL 0.25

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static double start_mono;
static double start_wall;
static int ready;          /* 1 initialised, -1 unusable */
static int active;         /* The slot file exists. */
static int borrowed;       /* Progress belongs to the parent (g.message -p). */
static int failed;
static pid_t writer;       /* Process that owns this state (not a fork). */
static pid_t owner;        /* Process the slot describes. */
static int last_pct = -2;
static long last_count = -1;
static double last_write;
static char dir[4096];
static char slot_path[4200];
static char name[256];
static char cmd[1024];
static char fail_msg[512];

static double mono_now(void)
{
    struct timespec ts;

    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static double wall_now(void)
{
    struct timespec ts;

    clock_gettime(CLOCK_REALTIME, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

__attribute__((constructor)) static void mm_start(void)
{
    start_mono = mono_now();
    start_wall = wall_now();
}

/* Read /proc/<pid>/cmdline; return the number of bytes (NUL separated). */
static size_t read_cmdline(pid_t pid, char *buf, size_t len)
{
    char path[64];
    FILE *f;
    size_t n;

    snprintf(path, sizeof(path), "/proc/%d/cmdline", (int)pid);
    f = fopen(path, "r");
    if (!f)
        return 0;
    n = fread(buf, 1, len - 1, f);
    fclose(f);
    buf[n] = '\0';
    return n;
}

static const char *base_name(const char *path)
{
    const char *slash = strrchr(path, '/');

    return slash ? slash + 1 : path;
}

static void make_dir(const char *path)
{
    if (mkdir(path, 0700) != 0 && errno != EEXIST)
        return;
}

static int init(void)
{
    const char *env, *prog;
    char raw[4096];
    size_t n, i;

    if (ready)
        return ready > 0;
    ready = -1;

    env = getenv("METAMONITOR_SPOOL");
    if (env && *env) {
        snprintf(dir, sizeof(dir), "%s", env);
    }
    else {
        env = getenv("XDG_RUNTIME_DIR");
        if (!env || !*env)
            return 0;
        snprintf(dir, sizeof(dir), "%s/metamonitor", env);
        make_dir(dir);
        snprintf(dir, sizeof(dir), "%s/metamonitor/slots", env);
    }
    make_dir(dir);

    writer = getpid();
    owner = writer;
    prog = program_invocation_short_name;
    if (strcmp(prog, "g.message") == 0) {
        borrowed = 1;
        owner = getppid();
    }

    n = read_cmdline(owner, raw, sizeof(raw));
    if (borrowed && n > 0) {
        /* "python3 /path/r.foo.py args": the script is the module. */
        const char *argv0 = raw, *argv1 = raw + strlen(raw) + 1;
        const char *argv2 = argv1 < raw + n ? argv1 + strlen(argv1) + 1 : argv1;

        if (strncmp(base_name(argv0), "python", 6) != 0 || argv1 >= raw + n)
            snprintf(name, sizeof(name), "%.255s", base_name(argv0));
        else if (strcmp(argv1, "-m") == 0 && argv2 < raw + n)
            snprintf(name, sizeof(name), "%.255s", argv2);
        else if (argv1[0] == '-')
            snprintf(name, sizeof(name), "%s", "python script");
        else
            snprintf(name, sizeof(name), "%.255s", base_name(argv1));
        if (strlen(name) > 3 && strcmp(name + strlen(name) - 3, ".py") == 0)
            name[strlen(name) - 3] = '\0';
    }
    else {
        snprintf(name, sizeof(name), "%s", prog);
    }
    for (i = 0; i + 1 < n; i++)
        if (raw[i] == '\0')
            raw[i] = ' ';
    snprintf(cmd, sizeof(cmd), "%s", n ? raw : name);

    snprintf(slot_path, sizeof(slot_path), "%s/grass-%d", dir, (int)owner);
    ready = 1;
    return 1;
}

static void json_str(FILE *f, const char *s)
{
    fputc('"', f);
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;

        if (c == '"' || c == '\\')
            fprintf(f, "\\%c", c);
        else if (c < 0x20)
            fprintf(f, "\\u%04x", c);
        else
            fputc(c, f);
    }
    fputc('"', f);
}

static void write_slot(int pct, long count, const char *state)
{
    char tmp[4300];
    const char *lock_pid = getenv("GIS_LOCK");
    FILE *f;
    double now = wall_now();

    snprintf(tmp, sizeof(tmp), "%s/.grass-%d.%d.tmp", dir, (int)owner,
             (int)getpid());
    f = fopen(tmp, "w");
    if (!f)
        return;
    fputs("{\"name\": ", f);
    json_str(f, name);
    fprintf(f, ", \"source\": \"grass\", \"group\": \"grass\", \"pid\": %d",
            (int)owner);
    if (pct >= 0)
        fprintf(f, ", \"percent\": %d", pct);
    else
        fputs(", \"percent\": null", f);
    fprintf(f, ", \"state\": \"%s\", \"detail\": ", state);
    if (failed) {
        json_str(f, fail_msg);
    }
    else if (count >= 0) {
        char buf[1100];

        snprintf(buf, sizeof(buf), "%ld processed · %s", count, cmd);
        json_str(f, buf);
    }
    else {
        json_str(f, cmd);
    }
    if (lock_pid && *lock_pid) {
        fputs(", \"parent\": ", f);
        char parent[64];

        snprintf(parent, sizeof(parent), "grass:%s", lock_pid);
        json_str(f, parent);
    }
    if (!borrowed)
        fprintf(f, ", \"started\": %.3f", start_wall);
    fprintf(f, ", \"updated\": %.3f", now);
    if (strcmp(state, "running") != 0)
        fprintf(f, ", \"finished\": %.3f", now);
    fputs("}\n", f);
    if (fclose(f) == 0 && rename(tmp, slot_path) == 0)
        active = 1;
    else
        unlink(tmp);
    last_write = mono_now();
}

/* Decide whether a progress value is worth writing, and write it. */
static void publish(int pct, long count)
{
    double t;
    int saved = errno;

    if (pthread_mutex_trylock(&lock) != 0)
        return;
    if (!init() || getpid() != writer)
        goto out;
    t = mono_now();
    if (!active) {
        if (borrowed) {
            /* Do not create a slot for a script that is already done. */
            if (pct >= 100 && access(slot_path, F_OK) != 0)
                goto out;
        }
        else if (t - start_mono < FIRST_DELAY) {
            goto out;
        }
    }
    if (pct >= 0) {
        if (pct == last_pct && active)
            goto out;
        if (active && pct < 100 && t - last_write < MIN_INTERVAL)
            goto out;
        last_pct = pct;
        write_slot(pct, -1, "running");
    }
    else {
        if (active && t - last_write < 1.0)
            goto out;
        last_count = count;
        write_slot(-1, count, "running");
    }
out:
    pthread_mutex_unlock(&lock);
    errno = saved;
}

void G_percent(long n, long d, int s)
{
    static void (*real)(long, long, int);

    if (!real)
        real = (void (*)(long, long, int))dlsym(RTLD_NEXT, "G_percent");
    publish((d <= 0 || s <= 0) ? 100 : (int)(100.0 * n / d), -1);
    if (real)
        real(n, d, s);
}

void G_progress(long n, int s)
{
    static void (*real)(long, int);

    if (!real)
        real = (void (*)(long, int))dlsym(RTLD_NEXT, "G_progress");
    if (n >= 0 && s > 0 && n % s == 0)
        publish(-1, n);
    if (real)
        real(n, s);
}

void G_fatal_error(const char *msg, ...)
{
    static void (*real)(const char *, ...);
    char buf[4096];
    va_list ap;

    va_start(ap, msg);
    vsnprintf(buf, sizeof(buf), msg, ap);
    va_end(ap);

    if (pthread_mutex_trylock(&lock) == 0) {
        if (active && getpid() == writer) {
            failed = 1;
            snprintf(fail_msg, sizeof(fail_msg), "ERROR: %.500s", buf);
            write_slot(last_pct, -1, "failed");
        }
        pthread_mutex_unlock(&lock);
    }
    if (!real)
        real = (void (*)(const char *, ...))dlsym(RTLD_NEXT, "G_fatal_error");
    if (real)
        real("%s", buf);
    fprintf(stderr, "ERROR: %s\n", buf);
    exit(EXIT_FAILURE);
}

__attribute__((destructor)) static void mm_finish(void)
{
    int saved = errno;

    if (active && !failed && !borrowed && getpid() == writer)
        write_slot(100, -1, "done");
    errno = saved;
}
