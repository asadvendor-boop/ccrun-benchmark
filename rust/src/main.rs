/*
 * ccrun - Coding Challenges Container Runtime (Rust Implementation)
 *
 * A minimal container runtime demonstrating Linux containerization primitives.
 * Built as part of John Crickett's Docker Coding Challenge (#52).
 *
 * This is the Rust implementation — demonstrating that even a "safe systems
 * language" produces identical container workload performance compared to
 * Python, Go, and C, because the Linux kernel does all the real work.
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
 */

use std::env;
use std::fs;
use std::io;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{self, Command};
use std::time::{SystemTime, UNIX_EPOCH};

use nix::mount::{mount, umount2, MntFlags, MsFlags};
use nix::sched::{unshare, CloneFlags};
use nix::unistd::{chroot, sethostname, fork, ForkResult};
use nix::sys::wait::waitpid;

use serde::{Deserialize, Serialize};

// ============================================================================
// Constants
// ============================================================================

const DEFAULT_HOSTNAME: &str = "container";
const DEFAULT_ROOTFS: &str = "/root/alpine-rootfs";
const IMAGES_DIR: &str = "/root/container-images";
const CGROUP_BASE: &str = "/sys/fs/cgroup";

const DOCKER_REGISTRY: &str = "https://registry-1.docker.io";
const DOCKER_AUTH: &str = "https://auth.docker.io";

// ============================================================================
// Image types for Step 7 & 8
// ============================================================================

#[derive(Deserialize, Debug)]
struct AuthResponse {
    token: String,
}

#[derive(Deserialize, Debug)]
struct ManifestLayer {
    #[serde(rename = "mediaType")]
    media_type: String,
    size: i64,
    digest: String,
}

#[derive(Deserialize, Debug)]
struct ManifestConfig {
    #[serde(rename = "mediaType")]
    media_type: Option<String>,
    size: Option<i64>,
    digest: String,
}

#[derive(Deserialize, Debug)]
struct Manifest {
    #[serde(rename = "schemaVersion")]
    schema_version: Option<i32>,
    config: Option<ManifestConfig>,
    layers: Option<Vec<ManifestLayer>>,
}

#[derive(Deserialize, Serialize, Debug, Clone)]
struct ContainerConfig {
    #[serde(rename = "Env", default)]
    env: Vec<String>,
    #[serde(rename = "Cmd", default)]
    cmd: Vec<String>,
    #[serde(rename = "Entrypoint", default)]
    entrypoint: Vec<String>,
    #[serde(rename = "WorkingDir", default)]
    working_dir: String,
}

#[derive(Deserialize, Serialize, Debug)]
struct ImageConfig {
    config: Option<ContainerConfig>,
}

// ============================================================================
// Main entry point
// ============================================================================

fn main() {
    let args: Vec<String> = env::args().collect();

    if args.len() < 2 {
        usage();
        process::exit(1);
    }

    match args[1].as_str() {
        "run" => run_container(&args[2..]),
        "child" => child_process(&args[2..]),
        "pull" => pull_cmd(&args[2..]),
        _ => {
            usage();
            process::exit(1);
        }
    }
}

fn usage() {
    eprintln!(
        "ccrun - Coding Challenges Container Runtime (Rust)

Usage:
  ccrun run [options] <command> [args...]
  ccrun pull <image>[:<tag>]

Options:
  --rootfs <path>    Root filesystem path (default: {})
  --hostname <name>  Container hostname (default: {})
  --memory <MB>      Memory limit in MB (default: 100)
  --cpu <quota>      CPU quota in us per 100ms (default: 50000)",
        DEFAULT_ROOTFS, DEFAULT_HOSTNAME
    );
}

// ============================================================================
// Step 1-6: Container launch
// ============================================================================

fn run_container(args: &[String]) {
    let mut rootfs = String::new();
    let mut hostname = DEFAULT_HOSTNAME.to_string();
    let mut memory_limit: u32 = 100;
    let mut cpu_quota: u32 = 50000;
    let mut command: Vec<String> = Vec::new();

    // Parse flags
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--rootfs" => {
                i += 1;
                if i < args.len() {
                    rootfs = args[i].clone();
                }
            }
            "--hostname" => {
                i += 1;
                if i < args.len() {
                    hostname = args[i].clone();
                }
            }
            "--memory" => {
                i += 1;
                if i < args.len() {
                    memory_limit = args[i].parse().unwrap_or(100);
                }
            }
            "--cpu" => {
                i += 1;
                if i < args.len() {
                    cpu_quota = args[i].parse().unwrap_or(50000);
                }
            }
            _ => {
                command = args[i..].to_vec();
                break;
            }
        }
        i += 1;
    }

    if command.is_empty() {
        eprintln!("Error: no command specified");
        process::exit(1);
    }

    // Determine rootfs
    let mut image_env: Vec<String> = Vec::new();
    let mut image_workdir = String::new();
    if rootfs.is_empty() {
        let image_rootfs = find_image_rootfs(&command[0]);
        if !image_rootfs.is_empty() {
            rootfs = image_rootfs;
            let image_name = command.remove(0);

            // Load image config for env and workdir
            if let Some(config) = load_image_config(&image_name) {
                if let Some(ref cc) = config.config {
                    image_env = cc.env.clone();
                    image_workdir = cc.working_dir.clone();
                    if command.is_empty() {
                        if !cc.entrypoint.is_empty() {
                            command = cc.entrypoint.clone();
                            command.extend(cc.cmd.clone());
                        } else if !cc.cmd.is_empty() {
                            command = cc.cmd.clone();
                        } else {
                            command = vec!["/bin/sh".to_string()];
                        }
                    }
                }
            }

            if command.is_empty() {
                command = vec!["/bin/sh".to_string()];
            }
        } else {
            rootfs = DEFAULT_ROOTFS.to_string();
        }
    }

    if command.is_empty() {
        command = vec!["/bin/sh".to_string()];
    }

    // Re-exec with "child" inside new namespaces
    let exe = fs::read_link("/proc/self/exe").unwrap_or_else(|_| PathBuf::from("./ccrun"));

    let mut child_args = vec![
        "child".to_string(),
        rootfs,
        hostname,
        memory_limit.to_string(),
        cpu_quota.to_string(),
    ];
    child_args.extend(command);

    let uid = nix::unistd::getuid();
    let gid = nix::unistd::getgid();

    // Clone values for use inside pre_exec closure
    let image_env_clone = image_env.clone();
    let image_workdir_clone = image_workdir.clone();

    let status = unsafe {
        Command::new(exe)
        .args(&child_args)
        .stdin(process::Stdio::inherit())
        .stdout(process::Stdio::inherit())
        .stderr(process::Stdio::inherit())
        .pre_exec(move || {
            // Create new namespaces using clone flags via unshare
            let mut flags = CloneFlags::CLONE_NEWUTS
                | CloneFlags::CLONE_NEWNS;

            // Only use user namespace for rootless mode
            if uid.as_raw() != 0 {
                flags |= CloneFlags::CLONE_NEWUSER;
            }

            unshare(flags).map_err(|e| io::Error::new(io::ErrorKind::Other, e))?;

            // Write UID/GID mappings (only needed for user namespace)
            if uid.as_raw() != 0 {
                let _ = fs::write("/proc/self/uid_map", format!("0 {} 1\n", uid));
                let _ = fs::write("/proc/self/setgroups", "deny\n");
                let _ = fs::write("/proc/self/gid_map", format!("0 {} 1\n", gid));
            }

            // Step 8: Set image env vars + workdir in environment
            for env_entry in &image_env_clone {
                if let Some((key, value)) = env_entry.split_once('=') {
                    std::env::set_var(key, value);
                }
            }
            if !image_workdir_clone.is_empty() {
                std::env::set_var("CCRUN_WORKDIR", &image_workdir_clone);
            }

            Ok(())
        })
        .status()
    };

    match status {
        Ok(s) => process::exit(s.code().unwrap_or(1)),
        Err(e) => {
            eprintln!("Error: {}", e);
            process::exit(1);
        }
    }
}

/// Runs inside the new namespaces (called via /proc/self/exe "child")
fn child_process(args: &[String]) {
    if args.len() < 5 {
        eprintln!("child: not enough arguments");
        process::exit(1);
    }

    let rootfs = &args[0];
    let hostname = &args[1];
    let memory_limit: u32 = args[2].parse().unwrap_or(100);
    let cpu_quota: u32 = args[3].parse().unwrap_or(50000);
    let command = &args[4..];

    let pid = process::id();
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let container_id = format!("ccrun-{}-{}", pid, timestamp);

    // Step 6: Set up cgroups (before fork so parent can clean up)
    let cgroup_path = setup_cgroups(&container_id, memory_limit, cpu_quota);

    // Create new PID namespace — the CHILD of this fork will be PID 1
    if let Err(e) = unshare(CloneFlags::CLONE_NEWPID) {
        eprintln!("unshare(CLONE_NEWPID): {}", e);
    }

    // Fork: child becomes PID 1 in the new PID namespace
    match unsafe { fork() } {
        Ok(ForkResult::Child) => {
            // We are PID 1 in the new PID namespace

            // Step 2: Set hostname
            if let Err(e) = sethostname(hostname) {
                eprintln!("sethostname: {}", e);
            }

            // Make mount namespace private
            let _ = mount(
                None::<&str>,
                "/",
                None::<&str>,
                MsFlags::MS_PRIVATE | MsFlags::MS_REC,
                None::<&str>,
            );

            // Step 3: Change root filesystem
            setup_rootfs(rootfs);

            // Step 4: Mount /proc (now shows PID 1)
            setup_mounts();

            // Step 8: Apply workdir from image config
            if let Ok(workdir) = env::var("CCRUN_WORKDIR") {
                if !workdir.is_empty() {
                    let _ = fs::create_dir_all(&workdir);
                    let _ = env::set_current_dir(&workdir);
                    env::remove_var("CCRUN_WORKDIR");
                }
            }

            // Exec the command
            let err = Command::new(&command[0]).args(&command[1..]).exec();
            eprintln!("exec {}: {}", command[0], err);
            process::exit(1);
        }
        Ok(ForkResult::Parent { child }) => {
            // Parent: wait for the container PID 1 to finish
            let _ = waitpid(child, None);
            cleanup_cgroups(&cgroup_path);
            process::exit(0);
        }
        Err(e) => {
            eprintln!("fork: {}", e);
            cleanup_cgroups(&cgroup_path);
            process::exit(1);
        }
    }
}

// ============================================================================
// Step 3: Filesystem isolation
// ============================================================================

fn setup_rootfs(rootfs: &str) {
    let rootfs_path = Path::new(rootfs);
    if !rootfs_path.is_dir() {
        eprintln!("rootfs not found: {}", rootfs);
        process::exit(1);
    }

    // Ensure essential directories exist
    for dir in &["proc", "sys", "dev", "tmp", "root"] {
        let _ = fs::create_dir_all(rootfs_path.join(dir));
    }

    // chroot to the new rootfs
    if let Err(e) = chroot(rootfs_path) {
        eprintln!("chroot: {}", e);
        process::exit(1);
    }

    if let Err(e) = env::set_current_dir("/") {
        eprintln!("chdir /: {}", e);
        process::exit(1);
    }
}

// ============================================================================
// Step 4: Mount /proc
// ============================================================================

fn setup_mounts() {
    let _ = mount(
        Some("proc"),
        "/proc",
        Some("proc"),
        MsFlags::MS_NOSUID | MsFlags::MS_NODEV | MsFlags::MS_NOEXEC,
        None::<&str>,
    );

    let _ = mount(
        Some("tmpfs"),
        "/tmp",
        Some("tmpfs"),
        MsFlags::empty(),
        None::<&str>,
    );
}

fn cleanup_mounts() {
    let _ = umount2("/proc", MntFlags::empty());
    let _ = umount2("/tmp", MntFlags::empty());
}

// ============================================================================
// Step 6: Cgroups
// ============================================================================

fn setup_cgroups(container_id: &str, memory_limit_mb: u32, cpu_quota: u32) -> String {
    let cgroup_path = format!("{}/ccrun/{}", CGROUP_BASE, container_id);

    let _ = fs::create_dir_all(&cgroup_path);

    // Memory limit
    let mem_bytes = (memory_limit_mb as u64) * 1024 * 1024;
    let _ = fs::write(format!("{}/memory.max", cgroup_path), mem_bytes.to_string());

    // CPU quota
    let _ = fs::write(
        format!("{}/cpu.max", cgroup_path),
        format!("{} 100000", cpu_quota),
    );

    // PID limit
    let _ = fs::write(format!("{}/pids.max", cgroup_path), "256");

    // Add current process
    let _ = fs::write(
        format!("{}/cgroup.procs", cgroup_path),
        process::id().to_string(),
    );

    cgroup_path
}

fn cleanup_cgroups(cgroup_path: &str) {
    let _ = fs::remove_dir(cgroup_path);
}

// ============================================================================
// Step 7: Pull images from Docker Hub
// ============================================================================

fn pull_cmd(args: &[String]) {
    if args.is_empty() {
        eprintln!("Usage: ccrun pull <image>[:<tag>]");
        process::exit(1);
    }

    let (image, tag) = parse_image_ref(&args[0]);

    match pull_image(&image, &tag) {
        Ok(_) => {}
        Err(e) => {
            eprintln!("Pull failed: {}", e);
            process::exit(1);
        }
    }
}

fn parse_image_ref(reference: &str) -> (String, String) {
    let parts: Vec<&str> = reference.splitn(2, ':').collect();
    let image = parts[0].to_string();
    let tag = if parts.len() > 1 {
        parts[1].to_string()
    } else {
        "latest".to_string()
    };
    (image, tag)
}

fn full_image_name(image: &str) -> String {
    if image.contains('/') {
        image.to_string()
    } else {
        format!("library/{}", image)
    }
}

fn authenticate(image: &str) -> Result<String, Box<dyn std::error::Error>> {
    let full_image = full_image_name(image);
    let url = format!(
        "{}/token?service=registry.docker.io&scope=repository:{}:pull",
        DOCKER_AUTH, full_image
    );

    let client = reqwest::blocking::Client::new();
    let resp: AuthResponse = client.get(&url).send()?.json()?;
    Ok(resp.token)
}

fn get_manifest(
    image: &str,
    tag: &str,
    token: &str,
) -> Result<Manifest, Box<dyn std::error::Error>> {
    let full_image = full_image_name(image);
    let url = format!("{}/v2/{}/manifests/{}", DOCKER_REGISTRY, full_image, tag);

    let client = reqwest::blocking::Client::new();
    let resp = client
        .get(&url)
        .header("Authorization", format!("Bearer {}", token))
        .header(
            "Accept",
            "application/vnd.docker.distribution.manifest.v2+json, \
             application/vnd.oci.image.manifest.v1+json",
        )
        .send()?;

    let manifest: Manifest = resp.json()?;
    Ok(manifest)
}

fn pull_layer(
    image: &str,
    digest: &str,
    token: &str,
    dest_dir: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let full_image = full_image_name(image);
    let url = format!(
        "{}/v2/{}/blobs/{}",
        DOCKER_REGISTRY, full_image, digest
    );

    let client = reqwest::blocking::Client::new();
    let resp = client
        .get(&url)
        .header("Authorization", format!("Bearer {}", token))
        .send()?;

    let gz = flate2::read::GzDecoder::new(resp);
    let mut archive = tar::Archive::new(gz);

    for entry in archive.entries()? {
        let mut entry = match entry {
            Ok(e) => e,
            Err(_) => continue,
        };

        let path = match entry.path() {
            Ok(p) => p.to_path_buf(),
            Err(_) => continue,
        };

        // Security: skip absolute paths and path traversal
        let path_str = path.to_string_lossy();
        if path_str.starts_with('/') || path_str.contains("..") {
            continue;
        }

        let target = Path::new(dest_dir).join(&path);
        let _ = entry.unpack(&target);
    }

    Ok(())
}

fn get_config(
    image: &str,
    digest: &str,
    token: &str,
) -> Result<ImageConfig, Box<dyn std::error::Error>> {
    let full_image = full_image_name(image);
    let url = format!(
        "{}/v2/{}/blobs/{}",
        DOCKER_REGISTRY, full_image, digest
    );

    let client = reqwest::blocking::Client::new();
    let resp = client
        .get(&url)
        .header("Authorization", format!("Bearer {}", token))
        .send()?;

    let config: ImageConfig = resp.json()?;
    Ok(config)
}

fn pull_image(image: &str, tag: &str) -> Result<(), Box<dyn std::error::Error>> {
    println!("Pulling {}:{}...", image, tag);

    let safe_image = image.replace('/', "_");
    let image_dir = format!("{}/{}/{}", IMAGES_DIR, safe_image, tag);
    let rootfs_dir = format!("{}/rootfs", image_dir);
    fs::create_dir_all(&rootfs_dir)?;

    // Authenticate
    println!("  Authenticating...");
    let token = authenticate(image)?;

    // Get manifest
    println!("  Fetching manifest...");
    let manifest = get_manifest(image, tag, &token)?;

    // Pull layers
    let layers = manifest.layers.unwrap_or_default();
    println!("  Found {} layers", layers.len());

    for layer in &layers {
        let short_digest = &layer.digest[..std::cmp::min(19, layer.digest.len())];
        println!("  Pulling layer {}...", short_digest);
        if let Err(e) = pull_layer(image, &layer.digest, &token, &rootfs_dir) {
            eprintln!("  Warning: layer {}: {}", short_digest, e);
        }
    }

    // Get and save config
    if let Some(ref config_info) = manifest.config {
        println!("  Fetching config...");
        match get_config(image, &config_info.digest, &token) {
            Ok(config) => {
                let config_json = serde_json::to_string_pretty(&config)?;
                fs::write(format!("{}/config.json", image_dir), config_json)?;
            }
            Err(e) => eprintln!("  Warning: config: {}", e),
        }
    }

    // Marker file
    fs::write(
        format!("{}/CONTAINER_IMAGE", rootfs_dir),
        format!("{}:{}\n", image, tag),
    )?;

    println!("  Image saved to {}", image_dir);
    println!("  Done! ✓");
    Ok(())
}

// ============================================================================
// Step 8: Run pulled images
// ============================================================================

fn find_image_rootfs(image: &str) -> String {
    let (image_name, tag) = parse_image_ref(image);
    let safe_image = image_name.replace('/', "_");
    let rootfs_dir = format!("{}/{}/{}/rootfs", IMAGES_DIR, safe_image, tag);

    if Path::new(&rootfs_dir).is_dir() {
        rootfs_dir
    } else {
        String::new()
    }
}

fn load_image_config(image: &str) -> Option<ImageConfig> {
    let (image_name, tag) = parse_image_ref(image);
    let safe_image = image_name.replace('/', "_");
    let config_path = format!("{}/{}/{}/config.json", IMAGES_DIR, safe_image, tag);

    let data = fs::read_to_string(config_path).ok()?;
    serde_json::from_str(&data).ok()
}
