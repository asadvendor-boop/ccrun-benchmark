/*
 * ccrun - Coding Challenges Container Runtime (C Implementation)
 *
 * A minimal container runtime demonstrating Linux containerization primitives.
 * Built as part of John Crickett's Docker Coding Challenge (#52).
 *
 * This is the C implementation — closest to the kernel, fewest abstractions.
 * We use it to demonstrate that even with minimal overhead, container workload
 * performance is identical to Python/Go/Rust because the kernel does the work.
 *
 * Steps implemented:
 *   1. Run an arbitrary command
 *   2. UTS namespace (hostname isolation)
 *   3. Filesystem isolation (chroot)
 *   4. PID namespace + /proc mount
 *   5. Rootless containers (user namespace)
 *   6. Cgroups (resource limits)
 *   7. Pull images from Docker Hub
 *   8. Run pulled images
 *
 * Compile:
 *   gcc -o ccrun ccrun.c -lcurl -ljson-c -Wall -Wextra -O2
 *
 * Usage:
 *   sudo ./ccrun run <command> [args...]
 *   sudo ./ccrun run --rootfs <path> <command> [args...]
 *   ./ccrun pull <image>[:<tag>]
 *   sudo ./ccrun run <image> <command> [args...]
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <signal.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <linux/limits.h>

#include <curl/curl.h>
#include <json-c/json.h>

/* ========================================================================== */
/* Constants                                                                   */
/* ========================================================================== */

#define DEFAULT_HOSTNAME   "container"
#define DEFAULT_ROOTFS     "/root/alpine-rootfs"
#define IMAGES_DIR         "/root/container-images"
#define CGROUP_BASE        "/sys/fs/cgroup"

#define DOCKER_REGISTRY    "https://registry-1.docker.io"
#define DOCKER_AUTH        "https://auth.docker.io"

#define STACK_SIZE         (1024 * 1024)  /* 1 MB child stack */
#define MAX_ARGS           64
#define MAX_LAYERS         128
#define CHUNK_SIZE         8192

/* ========================================================================== */
/* Utility functions                                                           */
/* ========================================================================== */

static void die(const char *msg) {
    fprintf(stderr, "ccrun: %s: %s\n", msg, strerror(errno));
    exit(1);
}

static void write_file(const char *path, const char *content) {
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) return;  /* silently ignore permission errors */
    write(fd, content, strlen(content));
    close(fd);
}

static void mkdirs(const char *path, mode_t mode) {
    char tmp[PATH_MAX];
    char *p = NULL;

    snprintf(tmp, sizeof(tmp), "%s", path);
    for (p = tmp + 1; *p; p++) {
        if (*p == '/') {
            *p = 0;
            mkdir(tmp, mode);
            *p = '/';
        }
    }
    mkdir(tmp, mode);
}

/* ========================================================================== */
/* Container configuration                                                     */
/* ========================================================================== */

struct container_config {
    char *rootfs;
    char *hostname;
    int   memory_limit_mb;
    int   cpu_quota;
    char *command[MAX_ARGS];
    int   command_count;
    char  container_id[128];
    char  cgroup_path[PATH_MAX];
    /* Step 8: image config */
    char *env_vars[MAX_ARGS];  /* KEY=VALUE strings */
    int   env_count;
    char  workdir[PATH_MAX];
};

/* ========================================================================== */
/* Step 6: Cgroups                                                             */
/* ========================================================================== */

static void setup_cgroups(struct container_config *cfg) {
    char path[PATH_MAX];
    char value[64];

    snprintf(cfg->cgroup_path, sizeof(cfg->cgroup_path),
             "%s/ccrun/%s", CGROUP_BASE, cfg->container_id);

    mkdirs(cfg->cgroup_path, 0755);

    /* Memory limit */
    long mem_bytes = (long)cfg->memory_limit_mb * 1024 * 1024;
    snprintf(path, sizeof(path), "%s/memory.max", cfg->cgroup_path);
    snprintf(value, sizeof(value), "%ld", mem_bytes);
    write_file(path, value);

    /* CPU quota */
    snprintf(path, sizeof(path), "%s/cpu.max", cfg->cgroup_path);
    snprintf(value, sizeof(value), "%d 100000", cfg->cpu_quota);
    write_file(path, value);

    /* PID limit */
    snprintf(path, sizeof(path), "%s/pids.max", cfg->cgroup_path);
    write_file(path, "256");

    /* Add current process */
    snprintf(path, sizeof(path), "%s/cgroup.procs", cfg->cgroup_path);
    snprintf(value, sizeof(value), "%d", getpid());
    write_file(path, value);
}

static void cleanup_cgroups(const char *cgroup_path) {
    rmdir(cgroup_path);
}

/* ========================================================================== */
/* Step 3: Filesystem isolation                                                */
/* ========================================================================== */

static void setup_rootfs(const char *rootfs) {
    struct stat st;
    if (stat(rootfs, &st) != 0 || !S_ISDIR(st.st_mode)) {
        fprintf(stderr, "ccrun: rootfs not found: %s\n", rootfs);
        exit(1);
    }

    /* Ensure essential directories exist */
    const char *dirs[] = {"proc", "sys", "dev", "tmp", "root", NULL};
    for (int i = 0; dirs[i]; i++) {
        char path[PATH_MAX];
        snprintf(path, sizeof(path), "%s/%s", rootfs, dirs[i]);
        mkdir(path, 0755);
    }

    /* chroot */
    if (chroot(rootfs) != 0) {
        die("chroot");
    }

    if (chdir("/") != 0) {
        die("chdir");
    }
}

/* ========================================================================== */
/* Step 4: Mount /proc                                                         */
/* ========================================================================== */

static void setup_mounts(void) {
    /* Mount proc */
    if (mount("proc", "/proc", "proc",
              MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL) != 0) {
        fprintf(stderr, "ccrun: mount /proc: %s\n", strerror(errno));
    }

    /* Mount tmpfs on /tmp */
    mount("tmpfs", "/tmp", "tmpfs", 0, NULL);
}

static void cleanup_mounts(void) {
    umount2("/proc", 0);
    umount2("/tmp", 0);
}

/* ========================================================================== */
/* Container child function (runs inside namespaces)                           */
/* ========================================================================== */

static int container_child(void *arg) {
    struct container_config *cfg = (struct container_config *)arg;

    /* Step 6: Set up cgroups */
    setup_cgroups(cfg);

    /* Step 2: Set hostname */
    if (sethostname(cfg->hostname, strlen(cfg->hostname)) != 0) {
        fprintf(stderr, "ccrun: sethostname: %s\n", strerror(errno));
    }

    /* Make mount namespace private */
    if (mount("", "/", "", MS_PRIVATE | MS_REC, NULL) != 0) {
        fprintf(stderr, "ccrun: mount private: %s\n", strerror(errno));
    }

    /* Step 3: Change root filesystem */
    setup_rootfs(cfg->rootfs);

    /* Step 4: Mount /proc */
    setup_mounts();

    /* Step 8: Apply image config (env vars + workdir) */
    for (int i = 0; i < cfg->env_count; i++) {
        if (cfg->env_vars[i]) {
            putenv(cfg->env_vars[i]);
        }
    }
    if (cfg->workdir[0] != '\0') {
        mkdirs(cfg->workdir, 0755);
        chdir(cfg->workdir);
    }

    /* Execute the command */
    execvp(cfg->command[0], cfg->command);

    /* Only reached if execvp fails */
    fprintf(stderr, "ccrun: exec %s: %s\n", cfg->command[0], strerror(errno));
    cleanup_mounts();
    cleanup_cgroups(cfg->cgroup_path);
    return 1;
}

/* ========================================================================== */
/* Step 5: User namespace setup                                                */
/* ========================================================================== */

static void setup_user_namespace(pid_t child_pid) {
    char path[PATH_MAX];
    char content[64];
    uid_t uid = getuid();
    gid_t gid = getgid();

    /* Write UID mapping */
    snprintf(path, sizeof(path), "/proc/%d/uid_map", child_pid);
    snprintf(content, sizeof(content), "0 %d 1\n", uid);
    write_file(path, content);

    /* Deny setgroups */
    snprintf(path, sizeof(path), "/proc/%d/setgroups", child_pid);
    write_file(path, "deny\n");

    /* Write GID mapping */
    snprintf(path, sizeof(path), "/proc/%d/gid_map", child_pid);
    snprintf(content, sizeof(content), "0 %d 1\n", gid);
    write_file(path, content);
}

/* ========================================================================== */
/* Step 1-6: Launch container                                                  */
/* ========================================================================== */

static int launch_container(struct container_config *cfg) {
    /* Generate container ID */
    snprintf(cfg->container_id, sizeof(cfg->container_id),
             "ccrun-%d-%ld", getpid(), (long)time(NULL));

    /* Allocate stack for clone */
    char *stack = malloc(STACK_SIZE);
    if (!stack) die("malloc stack");
    char *stack_top = stack + STACK_SIZE;

    /* Clone with new namespaces */
    int clone_flags = CLONE_NEWUTS | CLONE_NEWPID | CLONE_NEWNS |
                      CLONE_NEWUSER | SIGCHLD;

    pid_t child_pid = clone(container_child, stack_top, clone_flags, cfg);
    if (child_pid < 0) {
        die("clone");
    }

    /* Step 5: Set up user namespace mappings from parent */
    setup_user_namespace(child_pid);

    /* Wait for child */
    int status;
    waitpid(child_pid, &status, 0);

    /* Cleanup */
    cleanup_cgroups(cfg->cgroup_path);
    free(stack);

    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    return 1;
}

/* ========================================================================== */
/* HTTP helper (libcurl)                                                       */
/* ========================================================================== */

struct http_response {
    char *data;
    size_t size;
};

static size_t http_write_cb(void *contents, size_t size, size_t nmemb, void *userp) {
    size_t total = size * nmemb;
    struct http_response *resp = (struct http_response *)userp;

    char *ptr = realloc(resp->data, resp->size + total + 1);
    if (!ptr) return 0;

    resp->data = ptr;
    memcpy(&(resp->data[resp->size]), contents, total);
    resp->size += total;
    resp->data[resp->size] = '\0';
    return total;
}

/* File write callback for downloading layers */
static size_t file_write_cb(void *contents, size_t size, size_t nmemb, void *userp) {
    return fwrite(contents, size, nmemb, (FILE *)userp);
}

static struct http_response *http_get(const char *url, const char *auth_header,
                                       const char *accept_header) {
    struct http_response *resp = calloc(1, sizeof(struct http_response));
    if (!resp) return NULL;

    CURL *curl = curl_easy_init();
    if (!curl) {
        free(resp);
        return NULL;
    }

    struct curl_slist *headers = NULL;
    if (auth_header) {
        char hdr[2048];
        snprintf(hdr, sizeof(hdr), "Authorization: Bearer %s", auth_header);
        headers = curl_slist_append(headers, hdr);
    }
    if (accept_header) {
        char hdr[512];
        snprintf(hdr, sizeof(hdr), "Accept: %s", accept_header);
        headers = curl_slist_append(headers, hdr);
    }

    curl_easy_setopt(curl, CURLOPT_URL, url);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, http_write_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, resp);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "ccrun/1.0");

    CURLcode res = curl_easy_perform(curl);
    if (res != CURLE_OK) {
        fprintf(stderr, "curl: %s\n", curl_easy_strerror(res));
        free(resp->data);
        free(resp);
        resp = NULL;
    }

    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);
    return resp;
}

static int http_download(const char *url, const char *auth_token,
                          const char *output_path) {
    FILE *fp = fopen(output_path, "wb");
    if (!fp) return -1;

    CURL *curl = curl_easy_init();
    if (!curl) {
        fclose(fp);
        return -1;
    }

    struct curl_slist *headers = NULL;
    if (auth_token) {
        char hdr[2048];
        snprintf(hdr, sizeof(hdr), "Authorization: Bearer %s", auth_token);
        headers = curl_slist_append(headers, hdr);
    }

    curl_easy_setopt(curl, CURLOPT_URL, url);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, file_write_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, fp);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "ccrun/1.0");

    CURLcode res = curl_easy_perform(curl);

    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);
    fclose(fp);

    return (res == CURLE_OK) ? 0 : -1;
}

/* ========================================================================== */
/* Step 7: Pull images from Docker Hub                                         */
/* ========================================================================== */

static char *docker_authenticate(const char *image) {
    char full_image[256];
    if (strchr(image, '/') == NULL) {
        snprintf(full_image, sizeof(full_image), "library/%s", image);
    } else {
        snprintf(full_image, sizeof(full_image), "%s", image);
    }

    char url[1024];
    snprintf(url, sizeof(url),
             "%s/token?service=registry.docker.io&scope=repository:%s:pull",
             DOCKER_AUTH, full_image);

    struct http_response *resp = http_get(url, NULL, NULL);
    if (!resp) return NULL;

    /* Parse JSON to get token */
    struct json_object *root = json_tokener_parse(resp->data);
    free(resp->data);
    free(resp);

    if (!root) return NULL;

    struct json_object *token_obj;
    if (!json_object_object_get_ex(root, "token", &token_obj)) {
        json_object_put(root);
        return NULL;
    }

    char *token = strdup(json_object_get_string(token_obj));
    json_object_put(root);
    return token;
}

static struct json_object *get_manifest(const char *image, const char *tag,
                                         const char *token) {
    char full_image[256];
    if (strchr(image, '/') == NULL) {
        snprintf(full_image, sizeof(full_image), "library/%s", image);
    } else {
        snprintf(full_image, sizeof(full_image), "%s", image);
    }

    char url[1024];
    snprintf(url, sizeof(url), "%s/v2/%s/manifests/%s",
             DOCKER_REGISTRY, full_image, tag);

    struct http_response *resp = http_get(url, token,
        "application/vnd.docker.distribution.manifest.v2+json, "
        "application/vnd.oci.image.manifest.v1+json");

    if (!resp) return NULL;

    struct json_object *manifest = json_tokener_parse(resp->data);
    free(resp->data);
    free(resp);
    return manifest;
}

static int pull_layer(const char *image, const char *digest,
                       const char *token, const char *dest_dir) {
    char full_image[256];
    if (strchr(image, '/') == NULL) {
        snprintf(full_image, sizeof(full_image), "library/%s", image);
    } else {
        snprintf(full_image, sizeof(full_image), "%s", image);
    }

    char url[1024];
    snprintf(url, sizeof(url), "%s/v2/%s/blobs/%s",
             DOCKER_REGISTRY, full_image, digest);

    /* Download layer to temp file */
    char layer_path[PATH_MAX];
    snprintf(layer_path, sizeof(layer_path), "%s/layer.tar.gz", dest_dir);

    if (http_download(url, token, layer_path) != 0) {
        return -1;
    }

    /* Extract using tar command (simplest approach in C) */
    char cmd[PATH_MAX * 2];
    snprintf(cmd, sizeof(cmd), "tar -xzf '%s' -C '%s' 2>/dev/null", layer_path, dest_dir);
    system(cmd);

    unlink(layer_path);
    return 0;
}

static int pull_image(const char *image, const char *tag) {
    printf("Pulling %s:%s...\n", image, tag);

    /* Create image directory */
    char safe_image[256];
    snprintf(safe_image, sizeof(safe_image), "%s", image);
    for (char *p = safe_image; *p; p++) {
        if (*p == '/') *p = '_';
    }

    char image_dir[PATH_MAX];
    char rootfs_dir[PATH_MAX];
    snprintf(image_dir, sizeof(image_dir), "%s/%s/%s", IMAGES_DIR, safe_image, tag);
    snprintf(rootfs_dir, sizeof(rootfs_dir), "%s/rootfs", image_dir);
    mkdirs(rootfs_dir, 0755);

    /* Initialize libcurl */
    curl_global_init(CURL_GLOBAL_ALL);

    /* Authenticate */
    printf("  Authenticating...\n");
    char *token = docker_authenticate(image);
    if (!token) {
        fprintf(stderr, "  Authentication failed\n");
        curl_global_cleanup();
        return -1;
    }

    /* Get manifest */
    printf("  Fetching manifest...\n");
    struct json_object *manifest = get_manifest(image, tag, token);
    if (!manifest) {
        fprintf(stderr, "  Failed to get manifest\n");
        free(token);
        curl_global_cleanup();
        return -1;
    }

    /* Pull layers */
    struct json_object *layers;
    if (json_object_object_get_ex(manifest, "layers", &layers)) {
        int nlayers = json_object_array_length(layers);
        printf("  Found %d layers\n", nlayers);

        for (int i = 0; i < nlayers; i++) {
            struct json_object *layer = json_object_array_get_idx(layers, i);
            struct json_object *digest_obj;
            if (json_object_object_get_ex(layer, "digest", &digest_obj)) {
                const char *digest = json_object_get_string(digest_obj);
                char short_digest[20];
                snprintf(short_digest, sizeof(short_digest), "%.19s", digest);
                printf("  Pulling layer %s...\n", short_digest);
                pull_layer(image, digest, token, rootfs_dir);
            }
        }
    }

    /* Get and save config */
    struct json_object *config_obj;
    if (json_object_object_get_ex(manifest, "config", &config_obj)) {
        struct json_object *config_digest;
        if (json_object_object_get_ex(config_obj, "digest", &config_digest)) {
            printf("  Fetching config...\n");
            const char *digest = json_object_get_string(config_digest);

            char full_image[256];
            if (strchr(image, '/') == NULL) {
                snprintf(full_image, sizeof(full_image), "library/%s", image);
            } else {
                snprintf(full_image, sizeof(full_image), "%s", image);
            }

            char url[1024];
            snprintf(url, sizeof(url), "%s/v2/%s/blobs/%s",
                     DOCKER_REGISTRY, full_image, digest);

            struct http_response *resp = http_get(url, token, NULL);
            if (resp) {
                char config_path[PATH_MAX];
                snprintf(config_path, sizeof(config_path), "%s/config.json", image_dir);
                write_file(config_path, resp->data);
                free(resp->data);
                free(resp);
            }
        }
    }

    /* Marker file */
    char marker_path[PATH_MAX];
    char marker_content[256];
    snprintf(marker_path, sizeof(marker_path), "%s/CONTAINER_IMAGE", rootfs_dir);
    snprintf(marker_content, sizeof(marker_content), "%s:%s\n", image, tag);
    write_file(marker_path, marker_content);

    printf("  Image saved to %s\n", image_dir);
    printf("  Done! ✓\n");

    json_object_put(manifest);
    free(token);
    curl_global_cleanup();
    return 0;
}

/* ========================================================================== */
/* Step 8: Run pulled images                                                   */
/* ========================================================================== */

static int find_image_rootfs(const char *image, char *rootfs_out, size_t out_size) {
    char safe_image[256];
    const char *tag = "latest";
    char image_name[256];

    /* Parse image:tag */
    const char *colon = strchr(image, ':');
    if (colon) {
        snprintf(image_name, sizeof(image_name), "%.*s", (int)(colon - image), image);
        tag = colon + 1;
    } else {
        snprintf(image_name, sizeof(image_name), "%s", image);
    }

    snprintf(safe_image, sizeof(safe_image), "%s", image_name);
    for (char *p = safe_image; *p; p++) {
        if (*p == '/') *p = '_';
    }

    snprintf(rootfs_out, out_size, "%s/%s/%s/rootfs", IMAGES_DIR, safe_image, tag);

    struct stat st;
    return (stat(rootfs_out, &st) == 0 && S_ISDIR(st.st_mode)) ? 1 : 0;
}

/* Load image config.json and populate env_vars + workdir in cfg */
static void load_image_config(const char *image, struct container_config *cfg) {
    char safe_image[256];
    const char *tag = "latest";
    char image_name[256];

    const char *colon = strchr(image, ':');
    if (colon) {
        snprintf(image_name, sizeof(image_name), "%.*s", (int)(colon - image), image);
        tag = colon + 1;
    } else {
        snprintf(image_name, sizeof(image_name), "%s", image);
    }

    snprintf(safe_image, sizeof(safe_image), "%s", image_name);
    for (char *p = safe_image; *p; p++) {
        if (*p == '/') *p = '_';
    }

    char config_path[PATH_MAX];
    snprintf(config_path, sizeof(config_path), "%s/%s/%s/config.json",
             IMAGES_DIR, safe_image, tag);

    /* Read config file */
    FILE *fp = fopen(config_path, "r");
    if (!fp) return;

    fseek(fp, 0, SEEK_END);
    long fsize = ftell(fp);
    fseek(fp, 0, SEEK_SET);

    char *data = malloc(fsize + 1);
    if (!data) { fclose(fp); return; }
    fread(data, 1, fsize, fp);
    data[fsize] = '\0';
    fclose(fp);

    struct json_object *root = json_tokener_parse(data);
    free(data);
    if (!root) return;

    struct json_object *config_obj;
    if (!json_object_object_get_ex(root, "config", &config_obj)) {
        json_object_put(root);
        return;
    }

    /* Extract Env */
    struct json_object *env_arr;
    if (json_object_object_get_ex(config_obj, "Env", &env_arr)) {
        int n = json_object_array_length(env_arr);
        for (int i = 0; i < n && cfg->env_count < MAX_ARGS - 1; i++) {
            const char *entry = json_object_get_string(
                json_object_array_get_idx(env_arr, i));
            if (entry) {
                cfg->env_vars[cfg->env_count++] = strdup(entry);
            }
        }
    }

    /* Extract WorkingDir */
    struct json_object *workdir_obj;
    if (json_object_object_get_ex(config_obj, "WorkingDir", &workdir_obj)) {
        const char *wd = json_object_get_string(workdir_obj);
        if (wd && strlen(wd) > 0) {
            snprintf(cfg->workdir, sizeof(cfg->workdir), "%s", wd);
        }
    }

    json_object_put(root);
}

/* ========================================================================== */
/* CLI                                                                         */
/* ========================================================================== */

static void usage(void) {
    fprintf(stderr,
        "ccrun - Coding Challenges Container Runtime (C)\n"
        "\n"
        "Usage:\n"
        "  ccrun run [options] <command> [args...]\n"
        "  ccrun pull <image>[:<tag>]\n"
        "\n"
        "Options:\n"
        "  --rootfs <path>    Root filesystem path (default: %s)\n"
        "  --hostname <name>  Container hostname (default: %s)\n"
        "  --memory <MB>      Memory limit in MB (default: 100)\n"
        "  --cpu <quota>      CPU quota in us per 100ms (default: 50000)\n",
        DEFAULT_ROOTFS, DEFAULT_HOSTNAME);
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        usage();
        return 1;
    }

    if (strcmp(argv[1], "pull") == 0) {
        /* Step 7: Pull image */
        if (argc < 3) {
            fprintf(stderr, "Usage: ccrun pull <image>[:<tag>]\n");
            return 1;
        }

        const char *image_ref = argv[2];
        char image[256], tag[64];

        const char *colon = strchr(image_ref, ':');
        if (colon) {
            snprintf(image, sizeof(image), "%.*s", (int)(colon - image_ref), image_ref);
            snprintf(tag, sizeof(tag), "%s", colon + 1);
        } else {
            snprintf(image, sizeof(image), "%s", image_ref);
            snprintf(tag, sizeof(tag), "latest");
        }

        return pull_image(image, tag) == 0 ? 0 : 1;

    } else if (strcmp(argv[1], "run") == 0) {
        /* Steps 1-6, 8: Run container */
        struct container_config cfg = {0};
        cfg.rootfs = NULL;
        cfg.hostname = DEFAULT_HOSTNAME;
        cfg.memory_limit_mb = 100;
        cfg.cpu_quota = 50000;
        cfg.command_count = 0;

        /* Parse arguments */
        int i;
        for (i = 2; i < argc; i++) {
            if (strcmp(argv[i], "--rootfs") == 0 && i + 1 < argc) {
                cfg.rootfs = argv[++i];
            } else if (strcmp(argv[i], "--hostname") == 0 && i + 1 < argc) {
                cfg.hostname = argv[++i];
            } else if (strcmp(argv[i], "--memory") == 0 && i + 1 < argc) {
                cfg.memory_limit_mb = atoi(argv[++i]);
            } else if (strcmp(argv[i], "--cpu") == 0 && i + 1 < argc) {
                cfg.cpu_quota = atoi(argv[++i]);
            } else {
                break;
            }
        }

        /* Remaining args are the command */
        if (i >= argc) {
            fprintf(stderr, "Error: no command specified\n");
            return 1;
        }

        for (int j = 0; i < argc && j < MAX_ARGS - 1; i++, j++) {
            cfg.command[j] = argv[i];
            cfg.command_count++;
        }
        cfg.command[cfg.command_count] = NULL;

        /* Determine rootfs */
        if (cfg.rootfs == NULL) {
            /* Check if first arg is an image name (Step 8) */
            char image_rootfs[PATH_MAX];
            if (find_image_rootfs(cfg.command[0], image_rootfs, sizeof(image_rootfs))) {
                /* Load image config for env vars + workdir */
                load_image_config(cfg.command[0], &cfg);

                cfg.rootfs = strdup(image_rootfs);
                /* Shift command args (remove image name) */
                for (int j = 0; j < cfg.command_count; j++) {
                    cfg.command[j] = cfg.command[j + 1];
                }
                cfg.command_count--;

                if (cfg.command_count == 0) {
                    cfg.command[0] = "/bin/sh";
                    cfg.command[1] = NULL;
                    cfg.command_count = 1;
                }
            } else {
                cfg.rootfs = DEFAULT_ROOTFS;
            }
        }

        return launch_container(&cfg);

    } else {
        usage();
        return 1;
    }
}
