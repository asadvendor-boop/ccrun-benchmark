/*
ccrun - Coding Challenges Container Runtime (Go Implementation)

A minimal container runtime that demonstrates Linux containerization primitives.
Built as part of John Crickett's Docker Coding Challenge (#52).

This is the Go implementation — the same language Docker itself is written in.
We use it as the "reference" implementation alongside Python, Rust, and C
to empirically test whether the runtime language affects container performance.

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
  sudo ./ccrun run <command> [args...]
  sudo ./ccrun run --rootfs <path> <command> [args...]
  ./ccrun pull <image>[:<tag>]
  sudo ./ccrun run <image> <command> [args...]
*/
package main

import (
	"archive/tar"
	"compress/gzip"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// ============================================================================
// Constants
// ============================================================================

const (
	defaultHostname = "container"
	defaultRootfs   = "/root/alpine-rootfs"
	imagesDir       = "/root/container-images"
	cgroupBase      = "/sys/fs/cgroup"

	dockerRegistry = "https://registry-1.docker.io"
	dockerAuth     = "https://auth.docker.io"
)

// ============================================================================
// Step 7 & 8: Image types
// ============================================================================

// AuthResponse represents the Docker Hub auth token response.
type AuthResponse struct {
	Token string `json:"token"`
}

// Manifest represents a Docker image manifest.
type Manifest struct {
	SchemaVersion int    `json:"schemaVersion"`
	MediaType     string `json:"mediaType"`
	Config        struct {
		MediaType string `json:"mediaType"`
		Size      int64  `json:"size"`
		Digest    string `json:"digest"`
	} `json:"config"`
	Layers []struct {
		MediaType string `json:"mediaType"`
		Size      int64  `json:"size"`
		Digest    string `json:"digest"`
	} `json:"layers"`
}

// ImageConfig represents the container configuration from the image.
type ImageConfig struct {
	Config struct {
		Env        []string `json:"Env"`
		Cmd        []string `json:"Cmd"`
		Entrypoint []string `json:"Entrypoint"`
		WorkingDir string   `json:"WorkingDir"`
	} `json:"config"`
}

// ============================================================================
// Main entry point
// ============================================================================

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(1)
	}

	switch os.Args[1] {
	case "run":
		runContainer()
	case "child":
		// Internal: runs inside the new namespaces
		childProcess()
	case "pull":
		pullCmd()
	default:
		usage()
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprintf(os.Stderr, `ccrun - Coding Challenges Container Runtime (Go)

Usage:
  ccrun run [options] <command> [args...]
  ccrun pull <image>[:<tag>]

Options:
  --rootfs <path>    Root filesystem path (default: %s)
  --hostname <name>  Container hostname (default: %s)
  --memory <MB>      Memory limit in MB (default: 100)
  --cpu <quota>      CPU quota in us per 100ms (default: 50000)
`, defaultRootfs, defaultHostname)
}

// ============================================================================
// Step 1-6: Container launch
// ============================================================================

func runContainer() {
	args := os.Args[2:]
	rootfs := ""
	hostname := defaultHostname
	memoryLimit := 100
	cpuQuota := 50000
	var command []string

	// Parse flags
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--rootfs":
			i++
			if i < len(args) {
				rootfs = args[i]
			}
		case "--hostname":
			i++
			if i < len(args) {
				hostname = args[i]
			}
		case "--memory":
			i++
			if i < len(args) {
				memoryLimit, _ = strconv.Atoi(args[i])
			}
		case "--cpu":
			i++
			if i < len(args) {
				cpuQuota, _ = strconv.Atoi(args[i])
			}
		default:
			command = args[i:]
			i = len(args) // break out
		}
	}

	if len(command) == 0 {
		fmt.Fprintln(os.Stderr, "Error: no command specified")
		os.Exit(1)
	}

	// Determine rootfs
	var imageConfig *ImageConfig
	if rootfs == "" {
		// Check if first argument is an image name (Step 8)
		imageRootfs := findImageRootfs(command[0])
		if imageRootfs != "" {
			rootfs = imageRootfs
			imageName := command[0]
			command = command[1:]

			// Load image config
			imageConfig = loadImageConfig(imageName)
			if imageConfig != nil && len(command) == 0 {
				if len(imageConfig.Config.Entrypoint) > 0 {
					command = append(imageConfig.Config.Entrypoint, imageConfig.Config.Cmd...)
				} else if len(imageConfig.Config.Cmd) > 0 {
					command = imageConfig.Config.Cmd
				} else {
					command = []string{"/bin/sh"}
				}
			}
		} else {
			rootfs = defaultRootfs
		}
	}

	if len(command) == 0 {
		command = []string{"/bin/sh"}
	}

	// Re-exec ourselves with "child" command inside new namespaces
	// This is the Go idiom for creating containers — /proc/self/exe trick
	childArgs := []string{"child", rootfs, hostname,
		strconv.Itoa(memoryLimit), strconv.Itoa(cpuQuota)}
	childArgs = append(childArgs, command...)

	cmd := exec.Command("/proc/self/exe", childArgs...)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	// Step 8: Pass image config env vars + workdir to child via environment
	cmd.Env = os.Environ()
	if imageConfig != nil {
		for _, env := range imageConfig.Config.Env {
			cmd.Env = append(cmd.Env, env)
		}
		if imageConfig.Config.WorkingDir != "" {
			cmd.Env = append(cmd.Env, "CCRUN_WORKDIR="+imageConfig.Config.WorkingDir)
		}
	}

	// Step 2, 4, 5: Create new namespaces
	cmd.SysProcAttr = &syscall.SysProcAttr{
		Cloneflags: syscall.CLONE_NEWUTS |
			syscall.CLONE_NEWPID |
			syscall.CLONE_NEWNS |
			syscall.CLONE_NEWUSER,
		UidMappings: []syscall.SysProcIDMap{
			{ContainerID: 0, HostID: os.Getuid(), Size: 1},
		},
		GidMappings: []syscall.SysProcIDMap{
			{ContainerID: 0, HostID: os.Getgid(), Size: 1},
		},
	}

	if err := cmd.Run(); err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			os.Exit(exitErr.ExitCode())
		}
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
}

// childProcess runs inside the new namespaces.
// Called via /proc/self/exe "child" <rootfs> <hostname> <memory> <cpu> <cmd...>
func childProcess() {
	if len(os.Args) < 7 {
		fmt.Fprintln(os.Stderr, "child: not enough arguments")
		os.Exit(1)
	}

	rootfs := os.Args[2]
	hostname := os.Args[3]
	memoryLimit, _ := strconv.Atoi(os.Args[4])
	cpuQuota, _ := strconv.Atoi(os.Args[5])
	command := os.Args[6:]
	containerID := fmt.Sprintf("ccrun-%d-%d", os.Getpid(), time.Now().Unix())

	// Step 6: Set up cgroups
	cgroupPath := setupCgroups(containerID, memoryLimit, cpuQuota)

	// Step 2: Set hostname
	if err := syscall.Sethostname([]byte(hostname)); err != nil {
		fmt.Fprintf(os.Stderr, "sethostname: %v\n", err)
	}

	// Make mount namespace private
	if err := syscall.Mount("", "/", "", syscall.MS_PRIVATE|syscall.MS_REC, ""); err != nil {
		fmt.Fprintf(os.Stderr, "mount private: %v\n", err)
	}

	// Step 3: Change root filesystem
	setupRootfs(rootfs)

	// Step 4: Mount /proc
	setupMounts()

	// Step 8: Apply image config (workdir) inside container
	applyImageConfig()

	// Exec the command
	if err := syscall.Exec(command[0], command, os.Environ()); err != nil {
		fmt.Fprintf(os.Stderr, "exec %s: %v\n", command[0], err)
	}

	// Cleanup (only reached if exec fails)
	cleanupMounts()
	cleanupCgroups(cgroupPath)
	os.Exit(1)
}

// ============================================================================
// Step 3: Filesystem isolation
// ============================================================================

func setupRootfs(rootfs string) {
	if _, err := os.Stat(rootfs); os.IsNotExist(err) {
		fmt.Fprintf(os.Stderr, "rootfs not found: %s\n", rootfs)
		os.Exit(1)
	}

	// Ensure essential directories exist
	for _, dir := range []string{"proc", "sys", "dev", "tmp", "root"} {
		os.MkdirAll(filepath.Join(rootfs, dir), 0755)
	}

	// chroot to the new rootfs
	if err := syscall.Chroot(rootfs); err != nil {
		fmt.Fprintf(os.Stderr, "chroot: %v\n", err)
		os.Exit(1)
	}

	if err := os.Chdir("/"); err != nil {
		fmt.Fprintf(os.Stderr, "chdir: %v\n", err)
		os.Exit(1)
	}
}

// ============================================================================
// Step 4: Mount /proc
// ============================================================================

func setupMounts() {
	// Mount proc
	if err := syscall.Mount("proc", "/proc", "proc",
		syscall.MS_NOSUID|syscall.MS_NODEV|syscall.MS_NOEXEC, ""); err != nil {
		fmt.Fprintf(os.Stderr, "mount /proc: %v\n", err)
	}

	// Mount tmpfs on /tmp
	if err := syscall.Mount("tmpfs", "/tmp", "tmpfs", 0, ""); err != nil {
		// Non-fatal
	}
}

func cleanupMounts() {
	syscall.Unmount("/proc", 0)
	syscall.Unmount("/tmp", 0)
}

// ============================================================================
// Step 6: Cgroups
// ============================================================================

func setupCgroups(containerID string, memoryLimitMB int, cpuQuota int) string {
	cgroupPath := filepath.Join(cgroupBase, "ccrun", containerID)

	if err := os.MkdirAll(cgroupPath, 0755); err != nil {
		// Non-fatal: might not have permissions
		return cgroupPath
	}

	// Memory limit
	memBytes := memoryLimitMB * 1024 * 1024
	writeFile(filepath.Join(cgroupPath, "memory.max"), strconv.Itoa(memBytes))

	// CPU quota
	writeFile(filepath.Join(cgroupPath, "cpu.max"),
		fmt.Sprintf("%d 100000", cpuQuota))

	// PID limit
	writeFile(filepath.Join(cgroupPath, "pids.max"), "256")

	// Add this process
	writeFile(filepath.Join(cgroupPath, "cgroup.procs"), strconv.Itoa(os.Getpid()))

	return cgroupPath
}

func cleanupCgroups(cgroupPath string) {
	os.Remove(cgroupPath)
}

func writeFile(path, content string) {
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		// Silently ignore — might not have permissions
	}
}

// ============================================================================
// Step 7: Pull images from Docker Hub
// ============================================================================

func pullCmd() {
	if len(os.Args) < 3 {
		fmt.Fprintln(os.Stderr, "Usage: ccrun pull <image>[:<tag>]")
		os.Exit(1)
	}

	imageRef := os.Args[2]
	image, tag := parseImageRef(imageRef)

	if err := pullImage(image, tag); err != nil {
		fmt.Fprintf(os.Stderr, "Pull failed: %v\n", err)
		os.Exit(1)
	}
}

func parseImageRef(ref string) (string, string) {
	parts := strings.SplitN(ref, ":", 2)
	image := parts[0]
	tag := "latest"
	if len(parts) > 1 {
		tag = parts[1]
	}
	return image, tag
}

func fullImageName(image string) string {
	if !strings.Contains(image, "/") {
		return "library/" + image
	}
	return image
}

func authenticate(image string) (string, error) {
	fullImage := fullImageName(image)
	url := fmt.Sprintf("%s/token?service=registry.docker.io&scope=repository:%s:pull",
		dockerAuth, fullImage)

	resp, err := http.Get(url)
	if err != nil {
		return "", fmt.Errorf("auth request failed: %w", err)
	}
	defer resp.Body.Close()

	var auth AuthResponse
	if err := json.NewDecoder(resp.Body).Decode(&auth); err != nil {
		return "", fmt.Errorf("auth decode failed: %w", err)
	}

	return auth.Token, nil
}

func getManifest(image, tag, token string) (*Manifest, error) {
	fullImage := fullImageName(image)
	url := fmt.Sprintf("%s/v2/%s/manifests/%s", dockerRegistry, fullImage, tag)

	req, _ := http.NewRequest("GET", url, nil)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept",
		"application/vnd.docker.distribution.manifest.v2+json, "+
			"application/vnd.oci.image.manifest.v1+json")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("manifest request failed: %w", err)
	}
	defer resp.Body.Close()

	var manifest Manifest
	if err := json.NewDecoder(resp.Body).Decode(&manifest); err != nil {
		return nil, fmt.Errorf("manifest decode failed: %w", err)
	}

	return &manifest, nil
}

func pullLayer(image, digest, token, destDir string) error {
	fullImage := fullImageName(image)
	url := fmt.Sprintf("%s/v2/%s/blobs/%s", dockerRegistry, fullImage, digest)

	req, _ := http.NewRequest("GET", url, nil)
	req.Header.Set("Authorization", "Bearer "+token)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return fmt.Errorf("layer request failed: %w", err)
	}
	defer resp.Body.Close()

	// Decompress gzip
	gzReader, err := gzip.NewReader(resp.Body)
	if err != nil {
		return fmt.Errorf("gzip reader failed: %w", err)
	}
	defer gzReader.Close()

	// Extract tar
	tarReader := tar.NewReader(gzReader)
	for {
		header, err := tarReader.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return fmt.Errorf("tar read failed: %w", err)
		}

		// Security: skip absolute paths and path traversal
		if strings.HasPrefix(header.Name, "/") || strings.Contains(header.Name, "..") {
			continue
		}

		targetPath := filepath.Join(destDir, header.Name)

		switch header.Typeflag {
		case tar.TypeDir:
			os.MkdirAll(targetPath, os.FileMode(header.Mode))
		case tar.TypeReg:
			os.MkdirAll(filepath.Dir(targetPath), 0755)
			f, err := os.OpenFile(targetPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC,
				os.FileMode(header.Mode))
			if err != nil {
				continue
			}
			io.Copy(f, tarReader)
			f.Close()
		case tar.TypeSymlink:
			os.MkdirAll(filepath.Dir(targetPath), 0755)
			os.Symlink(header.Linkname, targetPath)
		case tar.TypeLink:
			os.MkdirAll(filepath.Dir(targetPath), 0755)
			linkPath := filepath.Join(destDir, header.Linkname)
			os.Link(linkPath, targetPath)
		}
	}

	return nil
}

func getConfig(image, digest, token string) (*ImageConfig, error) {
	fullImage := fullImageName(image)
	url := fmt.Sprintf("%s/v2/%s/blobs/%s", dockerRegistry, fullImage, digest)

	req, _ := http.NewRequest("GET", url, nil)
	req.Header.Set("Authorization", "Bearer "+token)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("config request failed: %w", err)
	}
	defer resp.Body.Close()

	var config ImageConfig
	if err := json.NewDecoder(resp.Body).Decode(&config); err != nil {
		return nil, fmt.Errorf("config decode failed: %w", err)
	}

	return &config, nil
}

func pullImage(image, tag string) error {
	fmt.Printf("Pulling %s:%s...\n", image, tag)

	// Create image directory
	safeImage := strings.ReplaceAll(image, "/", "_")
	imageDir := filepath.Join(imagesDir, safeImage, tag)
	rootfsDir := filepath.Join(imageDir, "rootfs")
	os.MkdirAll(rootfsDir, 0755)

	// Authenticate
	fmt.Println("  Authenticating...")
	token, err := authenticate(image)
	if err != nil {
		return err
	}

	// Get manifest
	fmt.Println("  Fetching manifest...")
	manifest, err := getManifest(image, tag, token)
	if err != nil {
		return err
	}

	// Pull layers
	fmt.Printf("  Found %d layers\n", len(manifest.Layers))
	for _, layer := range manifest.Layers {
		fmt.Printf("  Pulling layer %s...\n", layer.Digest[:19])
		if err := pullLayer(image, layer.Digest, token, rootfsDir); err != nil {
			fmt.Fprintf(os.Stderr, "  Warning: layer %s: %v\n", layer.Digest[:19], err)
		}
	}

	// Get and save config
	if manifest.Config.Digest != "" {
		fmt.Println("  Fetching config...")
		config, err := getConfig(image, manifest.Config.Digest, token)
		if err == nil {
			configData, _ := json.MarshalIndent(config, "", "  ")
			os.WriteFile(filepath.Join(imageDir, "config.json"), configData, 0644)
		}
	}

	// Marker file
	os.WriteFile(filepath.Join(rootfsDir, "CONTAINER_IMAGE"),
		[]byte(fmt.Sprintf("%s:%s\n", image, tag)), 0644)

	fmt.Printf("  Image saved to %s\n", imageDir)
	fmt.Println("  Done! ✓")
	return nil
}

// ============================================================================
// Step 8: Run pulled images
// ============================================================================

func findImageRootfs(image string) string {
	_, tag := parseImageRef(image)
	imageName := strings.SplitN(image, ":", 2)[0]
	safeImage := strings.ReplaceAll(imageName, "/", "_")
	rootfsDir := filepath.Join(imagesDir, safeImage, tag, "rootfs")

	if _, err := os.Stat(rootfsDir); err == nil {
		return rootfsDir
	}
	return ""
}

func loadImageConfig(image string) *ImageConfig {
	_, tag := parseImageRef(image)
	imageName := strings.SplitN(image, ":", 2)[0]
	safeImage := strings.ReplaceAll(imageName, "/", "_")
	configPath := filepath.Join(imagesDir, safeImage, tag, "config.json")

	data, err := os.ReadFile(configPath)
	if err != nil {
		return nil
	}

	var config ImageConfig
	if err := json.Unmarshal(data, &config); err != nil {
		return nil
	}

	return &config
}

func applyImageConfig() {
	// Step 8: Apply workdir passed from parent via CCRUN_WORKDIR env var
	workdir := os.Getenv("CCRUN_WORKDIR")
	if workdir != "" {
		os.MkdirAll(workdir, 0755)
		if err := os.Chdir(workdir); err != nil {
			fmt.Fprintf(os.Stderr, "chdir %s: %v\n", workdir, err)
		}
		// Clean up internal env var
		os.Unsetenv("CCRUN_WORKDIR")
	}
}
