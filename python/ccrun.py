#!/usr/bin/env python3
"""
ccrun - Coding Challenges Container Runtime (Python Implementation)

A minimal container runtime that demonstrates Linux containerization primitives.
Built as part of John Crickett's Docker Coding Challenge (#52).

This implementation proves that the language used to BUILD the container runtime
has zero impact on the PERFORMANCE of workloads running inside the container,
because all the actual work is done by the Linux kernel.

Steps implemented:
  1. Run an arbitrary command
  2. UTS namespace (hostname isolation)
  3. Filesystem isolation (chroot)
  4. PID namespace + /proc mount
  5. Rootless containers (user namespace)
  6. Cgroups (resource limits)
  7. Pull images from Docker Hub
  8. Run pulled images

Usage:
  sudo python3 ccrun.py run <command> [args...]
  sudo python3 ccrun.py run --rootfs <path> <command> [args...]
  python3 ccrun.py pull <image>[:<tag>]
  sudo python3 ccrun.py run <image> <command> [args...]
"""

import os
import sys
import socket
import struct
import ctypes
import ctypes.util
import signal
import json
import hashlib
import tarfile
import tempfile
import shutil
import time
import argparse
import urllib.request
import urllib.error

# ============================================================================
# Constants
# ============================================================================

CLONE_NEWUTS  = 0x04000000   # New UTS namespace (hostname)
CLONE_NEWPID  = 0x20000000   # New PID namespace
CLONE_NEWNS   = 0x00020000   # New mount namespace
CLONE_NEWUSER = 0x10000000   # New user namespace
CLONE_NEWNET  = 0x40000000   # New network namespace

MS_NOSUID    = 2
MS_NODEV     = 4
MS_NOEXEC    = 8
MS_PRIVATE   = 1 << 18
MS_REC       = 16384

STACK_SIZE   = 1024 * 1024   # 1 MB child stack

DEFAULT_HOSTNAME = "container"
DEFAULT_ROOTFS   = os.path.expanduser("~/alpine-rootfs")
IMAGES_DIR       = os.path.expanduser("~/container-images")

DOCKER_REGISTRY  = "https://registry-1.docker.io"
DOCKER_AUTH       = "https://auth.docker.io"

# ============================================================================
# Low-level Linux syscall wrappers via ctypes
# ============================================================================

libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

def _check_err(ret, msg="syscall failed"):
    """Check return value of a syscall and raise on error."""
    if ret < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"{msg}: {os.strerror(errno)}")
    return ret

def sethostname(hostname: str):
    """Set the system hostname (requires CAP_SYS_ADMIN in UTS namespace)."""
    name = hostname.encode()
    ret = libc.sethostname(name, len(name))
    _check_err(ret, "sethostname")

def unshare(flags: int):
    """Disassociate parts of the process execution context."""
    ret = libc.unshare(flags)
    _check_err(ret, f"unshare(0x{flags:08x})")

def mount(source: str, target: str, fstype: str, flags: int = 0, data: str = ""):
    """Mount a filesystem."""
    ret = libc.mount(
        source.encode() if source else None,
        target.encode(),
        fstype.encode() if fstype else None,
        flags,
        data.encode() if data else None,
    )
    _check_err(ret, f"mount({source}, {target}, {fstype})")

def umount2(target: str, flags: int = 0):
    """Unmount a filesystem."""
    ret = libc.umount2(target.encode(), flags)
    # Don't fail on umount errors during cleanup
    return ret

def pivot_root(new_root: str, put_old: str):
    """Change the root filesystem."""
    SYS_PIVOT_ROOT = 217  # ARM64 syscall number
    ret = libc.syscall(SYS_PIVOT_ROOT, new_root.encode(), put_old.encode())
    _check_err(ret, f"pivot_root({new_root}, {put_old})")

def chroot(path: str):
    """Change root directory."""
    ret = libc.chroot(path.encode())
    _check_err(ret, f"chroot({path})")

# ============================================================================
# Step 1: Run an arbitrary command
# ============================================================================

def run_command(args: list) -> int:
    """Fork and exec a command, returning its exit code."""
    pid = os.fork()
    if pid == 0:
        # Child process
        try:
            os.execvp(args[0], args)
        except Exception as e:
            print(f"exec failed: {e}", file=sys.stderr)
            os._exit(127)
    else:
        # Parent - wait for child
        _, status = os.waitpid(pid, 0)
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        return 1

# ============================================================================
# Step 2: UTS Namespace (hostname isolation)
# ============================================================================

def setup_hostname(hostname: str = DEFAULT_HOSTNAME):
    """Set container hostname within UTS namespace."""
    sethostname(hostname)
    # Also set in environment for shell prompts
    os.environ["HOSTNAME"] = hostname

# ============================================================================
# Step 3: Filesystem isolation (chroot)
# ============================================================================

def setup_rootfs(rootfs_path: str):
    """
    Change root filesystem to the given path.
    Uses chroot for simplicity (pivot_root would be more secure).
    """
    if not os.path.isdir(rootfs_path):
        print(f"Error: rootfs path '{rootfs_path}' does not exist", file=sys.stderr)
        sys.exit(1)

    # Ensure essential directories exist
    for d in ["proc", "sys", "dev", "tmp", "root"]:
        os.makedirs(os.path.join(rootfs_path, d), exist_ok=True)

    chroot(rootfs_path)
    os.chdir("/")

# ============================================================================
# Step 4: PID namespace + /proc mount
# ============================================================================

def setup_mounts():
    """Mount /proc and /sys inside the container."""
    # Mount proc filesystem
    mount("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC)

    # Mount tmpfs on /tmp
    mount("tmpfs", "/tmp", "tmpfs", 0)

def cleanup_mounts():
    """Unmount filesystems on container exit."""
    umount2("/proc", 0)
    umount2("/tmp", 0)

# ============================================================================
# Step 5: User namespace (rootless containers)
# ============================================================================

def setup_user_namespace():
    """
    Map the current user to root inside the container.
    This is done BEFORE other namespace setup when using clone().
    For the unshare approach, we write the mappings after unshare.
    """
    uid = os.getuid()
    gid = os.getgid()

    # Write UID mapping: container root (0) maps to our UID
    try:
        with open("/proc/self/uid_map", "w") as f:
            f.write(f"0 {uid} 1\n")
    except PermissionError:
        pass  # May need to disable setgroups first

    # Disable setgroups (required before writing gid_map as unprivileged user)
    try:
        with open("/proc/self/setgroups", "w") as f:
            f.write("deny\n")
    except (FileNotFoundError, PermissionError):
        pass

    # Write GID mapping
    try:
        with open("/proc/self/gid_map", "w") as f:
            f.write(f"0 {gid} 1\n")
    except PermissionError:
        pass

# ============================================================================
# Step 6: Cgroups (resource limits)
# ============================================================================

CGROUP_BASE = "/sys/fs/cgroup"

def setup_cgroups(container_id: str, memory_limit_mb: int = 100, cpu_quota: int = 50000):
    """
    Set up cgroup v2 resource limits for the container.

    Args:
        container_id: Unique identifier for the cgroup
        memory_limit_mb: Memory limit in megabytes
        cpu_quota: CPU quota in microseconds per 100ms period
    """
    cgroup_path = os.path.join(CGROUP_BASE, "ccrun", container_id)

    try:
        os.makedirs(cgroup_path, exist_ok=True)

        # Set memory limit
        mem_limit = memory_limit_mb * 1024 * 1024  # Convert to bytes
        with open(os.path.join(cgroup_path, "memory.max"), "w") as f:
            f.write(str(mem_limit))

        # Set CPU quota (microseconds per 100ms period)
        with open(os.path.join(cgroup_path, "cpu.max"), "w") as f:
            f.write(f"{cpu_quota} 100000")

        # Set max number of PIDs
        with open(os.path.join(cgroup_path, "pids.max"), "w") as f:
            f.write("256")

        # Add current process to the cgroup
        with open(os.path.join(cgroup_path, "cgroup.procs"), "w") as f:
            f.write(str(os.getpid()))

    except PermissionError:
        print("Warning: Could not set cgroup limits (need root)", file=sys.stderr)
    except FileNotFoundError:
        print("Warning: cgroup v2 not available", file=sys.stderr)

    return cgroup_path

def cleanup_cgroups(cgroup_path: str):
    """Remove the cgroup directory."""
    try:
        if os.path.isdir(cgroup_path):
            os.rmdir(cgroup_path)
    except OSError:
        pass

# ============================================================================
# Step 7: Pull images from Docker Hub
# ============================================================================

def docker_auth(image: str) -> str:
    """Get an authentication token for Docker Hub."""
    # Parse image name
    if "/" not in image:
        image = f"library/{image}"

    url = (
        f"{DOCKER_AUTH}/token?"
        f"service=registry.docker.io&"
        f"scope=repository:{image}:pull"
    )

    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
        return data["token"]

def get_manifest(image: str, tag: str, token: str) -> dict:
    """Fetch the image manifest from Docker Hub."""
    if "/" not in image:
        image = f"library/{image}"

    url = f"{DOCKER_REGISTRY}/v2/{image}/manifests/{tag}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": (
            "application/vnd.docker.distribution.manifest.v2+json, "
            "application/vnd.oci.image.manifest.v1+json"
        ),
    })

    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

def pull_layer(image: str, digest: str, token: str, dest_dir: str):
    """Download and extract a single image layer."""
    if "/" not in image:
        image = f"library/{image}"

    url = f"{DOCKER_REGISTRY}/v2/{image}/blobs/{digest}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
    })

    print(f"  Pulling layer {digest[:19]}...")

    with urllib.request.urlopen(req) as resp:
        # Save to a temp file then extract
        layer_file = os.path.join(dest_dir, "layer.tar.gz")
        with open(layer_file, "wb") as f:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                f.write(chunk)

        # Extract the layer
        try:
            with tarfile.open(layer_file, "r:gz") as tar:
                # Filter out potentially dangerous paths
                members = []
                for member in tar.getmembers():
                    if member.name.startswith("/") or ".." in member.name:
                        continue
                    members.append(member)
                tar.extractall(path=dest_dir, members=members)
        except tarfile.ReadError:
            # Some layers might be gzip without tar
            pass

        os.remove(layer_file)

def get_config(image: str, digest: str, token: str) -> dict:
    """Fetch the image configuration."""
    if "/" not in image:
        image = f"library/{image}"

    url = f"{DOCKER_REGISTRY}/v2/{image}/blobs/{digest}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
    })

    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

def pull_image(image: str, tag: str = "latest"):
    """
    Pull a container image from Docker Hub.

    Downloads all layers and unpacks them to create the rootfs.
    """
    print(f"Pulling {image}:{tag}...")

    # Create image directory
    image_dir = os.path.join(IMAGES_DIR, image.replace("/", "_"), tag)
    rootfs_dir = os.path.join(image_dir, "rootfs")
    os.makedirs(rootfs_dir, exist_ok=True)

    # Authenticate
    print("  Authenticating...")
    token = docker_auth(image)

    # Get manifest
    print("  Fetching manifest...")
    manifest = get_manifest(image, tag, token)

    # Download and extract layers (in order, base first)
    layers = manifest.get("layers", [])
    print(f"  Found {len(layers)} layers")

    for layer in layers:
        digest = layer["digest"]
        pull_layer(image, digest, token, rootfs_dir)

    # Get and save config
    config_digest = manifest.get("config", {}).get("digest", "")
    if config_digest:
        print("  Fetching config...")
        config = get_config(image, config_digest, token)
        config_path = os.path.join(image_dir, "config.json")
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)

    # Create marker file
    with open(os.path.join(rootfs_dir, "CONTAINER_IMAGE"), "w") as f:
        f.write(f"{image}:{tag}\n")

    print(f"  Image saved to {image_dir}")
    print(f"  Done! ✓")
    return image_dir

# ============================================================================
# Step 8: Run pulled images
# ============================================================================

def find_image_rootfs(image: str, tag: str = "latest") -> str:
    """Find the rootfs directory for a pulled image."""
    image_dir = os.path.join(IMAGES_DIR, image.replace("/", "_"), tag)
    rootfs_dir = os.path.join(image_dir, "rootfs")

    if not os.path.isdir(rootfs_dir):
        return ""

    return rootfs_dir

def load_image_config(image: str, tag: str = "latest") -> dict:
    """Load the saved image configuration."""
    image_dir = os.path.join(IMAGES_DIR, image.replace("/", "_"), tag)
    config_path = os.path.join(image_dir, "config.json")

    if not os.path.isfile(config_path):
        return {}

    with open(config_path) as f:
        return json.load(f)

def apply_image_config(config: dict) -> tuple:
    """Apply image env vars and return workdir for later use inside container."""
    container_config = config.get("config", config.get("container_config", {}))

    # Set environment variables (inherited by child via fork)
    env_list = container_config.get("Env", [])
    for env_entry in env_list:
        if "=" in env_entry:
            key, value = env_entry.split("=", 1)
            os.environ[key] = value

    # Return workdir — must be applied AFTER chroot inside container
    workdir = container_config.get("WorkingDir", "")
    return workdir

# ============================================================================
# Container entry point (runs inside namespaces)
# ============================================================================

def container_init(rootfs: str, command: list, hostname: str,
                   memory_limit: int, cpu_quota: int, container_id: str,
                   workdir: str = ""):
    """
    This function runs INSIDE the new namespaces.
    It sets up the container environment and executes the target command.
    """
    cgroup_path = None

    try:
        # Step 6: Set up cgroups
        cgroup_path = setup_cgroups(container_id, memory_limit, cpu_quota)

        # Step 2: Set hostname
        setup_hostname(hostname)

        # Step 4: Unshare mount namespace so our mounts are private
        unshare(CLONE_NEWNS)

        # Make all mounts private (don't propagate to host)
        mount("", "/", "", MS_PRIVATE | MS_REC)

        # Step 3: Change root filesystem
        setup_rootfs(rootfs)

        # Step 4: Mount /proc inside container
        setup_mounts()

        # Step 8: Apply workdir AFTER chroot (so paths resolve inside container)
        if workdir:
            try:
                os.chdir(workdir)
            except FileNotFoundError:
                os.makedirs(workdir, exist_ok=True)
                os.chdir(workdir)

        # Execute the command
        os.execvp(command[0], command)

    except Exception as e:
        print(f"Container error: {e}", file=sys.stderr)
        return 1

    finally:
        # Cleanup (only reached if execvp fails)
        cleanup_mounts()
        if cgroup_path:
            cleanup_cgroups(cgroup_path)

    return 1

# ============================================================================
# Main container launch logic
# ============================================================================

def launch_container(rootfs: str, command: list, hostname: str = DEFAULT_HOSTNAME,
                     memory_limit: int = 100, cpu_quota: int = 50000,
                     workdir: str = ""):
    """
    Launch a containerized process with full isolation:
    - UTS namespace (hostname)
    - PID namespace (process isolation)
    - Mount namespace (filesystem isolation)
    - User namespace (rootless)
    - chroot (filesystem boundary)
    - cgroups (resource limits)
    """
    container_id = f"ccrun-{os.getpid()}-{int(time.time())}"

    # We use unshare + fork approach instead of clone() for simplicity in Python
    # The child will get new namespaces

    # First, unshare UTS and PID namespaces
    # PID namespace takes effect for children of this process
    clone_flags = CLONE_NEWUTS | CLONE_NEWPID | CLONE_NEWNS

    # Try to use user namespace for rootless (Step 5)
    if os.getuid() != 0:
        clone_flags |= CLONE_NEWUSER

    unshare(clone_flags)

    # Set up user namespace mappings if unprivileged
    if os.getuid() != 0:
        setup_user_namespace()

    # Fork — child gets PID 1 in new PID namespace
    pid = os.fork()

    if pid == 0:
        # ---- CHILD: runs inside new namespaces ----
        exit_code = container_init(
            rootfs, command, hostname,
            memory_limit, cpu_quota, container_id,
            workdir=workdir
        )
        os._exit(exit_code)
    else:
        # ---- PARENT: wait for container to finish ----
        _, status = os.waitpid(pid, 0)

        # Clean up cgroup
        cgroup_path = os.path.join(CGROUP_BASE, "ccrun", container_id)
        cleanup_cgroups(cgroup_path)

        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        return 1

# ============================================================================
# CLI Interface
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        prog="ccrun",
        description="Coding Challenges Container Runtime (Python)"
    )
    subparsers = parser.add_subparsers(dest="action", help="Action to perform")

    # --- run command ---
    run_parser = subparsers.add_parser("run", help="Run a command in a container")
    run_parser.add_argument("--rootfs", default="", help="Root filesystem path")
    run_parser.add_argument("--hostname", default=DEFAULT_HOSTNAME, help="Container hostname")
    run_parser.add_argument("--memory", type=int, default=100, help="Memory limit in MB")
    run_parser.add_argument("--cpu", type=int, default=50000, help="CPU quota (us per 100ms)")
    run_parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to run")

    # --- pull command ---
    pull_parser = subparsers.add_parser("pull", help="Pull an image from Docker Hub")
    pull_parser.add_argument("image", help="Image name (e.g., alpine, ubuntu)")
    pull_parser.add_argument("--tag", default="latest", help="Image tag")

    args = parser.parse_args()

    if args.action == "pull":
        # Step 7: Pull image
        try:
            pull_image(args.image, args.tag)
            sys.exit(0)
        except Exception as e:
            print(f"Pull failed: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.action == "run":
        if not args.command:
            print("Error: no command specified", file=sys.stderr)
            sys.exit(1)

        # Filter out '--' from command if present
        command = [c for c in args.command if c != "--"]

        # Determine rootfs
        rootfs = args.rootfs

        workdir = ""

        if not rootfs:
            # Check if first argument is an image name (Step 8)
            potential_image = command[0]
            image_rootfs = find_image_rootfs(potential_image)
            if image_rootfs:
                rootfs = image_rootfs
                command = command[1:]  # Remove image name from command

                # Load and apply image config
                config = load_image_config(potential_image)
                if config:
                    workdir = apply_image_config(config)

                    # If no command specified, use image's default CMD
                    if not command:
                        container_config = config.get("config",
                                            config.get("container_config", {}))
                        cmd = container_config.get("Cmd", ["/bin/sh"])
                        entrypoint = container_config.get("Entrypoint", [])
                        command = entrypoint + cmd if entrypoint else cmd
            else:
                rootfs = DEFAULT_ROOTFS

        if not command:
            command = ["/bin/sh"]

        # Launch container
        exit_code = launch_container(
            rootfs=rootfs,
            command=command,
            hostname=args.hostname,
            memory_limit=args.memory,
            cpu_quota=args.cpu,
            workdir=workdir,
        )
        sys.exit(exit_code)

    else:
        parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()
