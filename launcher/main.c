/*
 * WeChatBackup - launcher for the nightly wechat-decrypt-export backup.
 *
 * Its only job: run  <REPO_ROOT>/venv/bin/python -E -s <REPO_ROOT>/backup.py --scheduled
 * as a child process (posix_spawn, not exec) and wait for it. Because this app is
 * the responsible process, macOS privacy checks (TCC) for the child's file access
 * are attributed to WeChatBackup.app, so Full Disk Access can be granted to this
 * app alone instead of to the Python interpreter (which would cover every script).
 *
 * REPO_ROOT is compiled in (build.sh); the program and script cannot be changed via
 * argv or the environment. The only accepted argument is "--dir <path>" (the export
 * directory). The child gets a minimal fixed environment (-E ignores PYTHON* vars,
 * -s ignores the user site-packages). stdout/stderr are inherited; the child's exit
 * status is returned (128+N if it was killed by signal N). SIGTERM/SIGINT/SIGHUP
 * received by the launcher (e.g. launchctl bootout) are forwarded to the child.
 */
#include <errno.h>
#include <pwd.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef REPO_ROOT
#error "build with -DREPO_ROOT=\"/path/to/repo\""
#endif

static volatile sig_atomic_t child_pid = 0;

static void forward(int sig) {
    if (child_pid > 0) kill(child_pid, sig);
}

int main(int argc, char **argv) {
    const char *dir = NULL;
    if (argc == 3 && strcmp(argv[1], "--dir") == 0 && argv[2][0] == '/') {
        dir = argv[2];
    } else if (argc != 1) {
        fprintf(stderr, "usage: WeChatBackup [--dir /absolute/export/dir]\n");
        return 2;
    }

    const char *python = REPO_ROOT "/venv/bin/python";
    const char *script = REPO_ROOT "/backup.py";
    char *args[8];
    int n = 0;
    args[n++] = (char *)python;
    args[n++] = "-E";
    args[n++] = "-s";
    args[n++] = (char *)script;
    args[n++] = "--scheduled";
    if (dir) {
        args[n++] = "--dir";
        args[n++] = (char *)dir;
    }
    args[n] = NULL;

    const char *home = getenv("HOME");
    struct passwd *pw = getpwuid(getuid());
    if (pw && pw->pw_dir) home = pw->pw_dir;
    static char home_env[1024], user_env[256], tmp_env[1024];
    snprintf(home_env, sizeof home_env, "HOME=%s", home ? home : "/");
    snprintf(user_env, sizeof user_env, "USER=%s", pw && pw->pw_name ? pw->pw_name : "");
    const char *tmpdir = getenv("TMPDIR");
    snprintf(tmp_env, sizeof tmp_env, "TMPDIR=%s", tmpdir && tmpdir[0] == '/' ? tmpdir : "/tmp/");
    char *env[] = {
        "PATH=/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG=en_US.UTF-8",
        "PYTHONIOENCODING=utf-8",
        "PYTHONUNBUFFERED=1",
        home_env, user_env, tmp_env,
        NULL,
    };

    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = forward;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);

    if (chdir(REPO_ROOT) != 0) {
        fprintf(stderr, "WeChatBackup: cannot chdir to %s: %s\n", REPO_ROOT, strerror(errno));
        return 1;
    }
    pid_t pid;
    int rc = posix_spawn(&pid, python, NULL, NULL, args, env);
    if (rc != 0) {
        fprintf(stderr, "WeChatBackup: cannot start %s: %s\n", python, strerror(rc));
        return 1;
    }
    child_pid = pid;

    int status;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) {
            perror("WeChatBackup: waitpid");
            return 1;
        }
    }
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
    return 1;
}
