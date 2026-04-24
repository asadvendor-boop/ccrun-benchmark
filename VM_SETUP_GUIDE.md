# Linux VM Setup Guide for ccrun-benchmark

## Step 1: Download Ubuntu Server ARM64

Download the ISO from your browser:
**https://cdimage.ubuntu.com/releases/24.04/release/ubuntu-24.04.2-live-server-arm64.iso**

(~2.6 GB download — start this first while you read the rest)

## Step 2: Create VM in UTM

1. Open **UTM** app
2. Click **"Create a New Virtual Machine"** (the "+" button)
3. Select **"Virtualize"** (NOT Emulate — this gives near-native performance)
4. Select **"Linux"**
5. **Boot ISO Image**: Browse and select the Ubuntu ISO you downloaded
6. Configure:
   - **Memory**: 4096 MB (4 GB)
   - **CPU Cores**: 4
   - **Storage**: 30 GB
   - **Shared Directory**: Select this folder:
     `/Users/asad.ali/.gemini/antigravity/playground/ancient-armstrong/ccrun-benchmark`
7. Click **"Save"** then **"Start"**

## Step 3: Install Ubuntu

1. Select **"Try or Install Ubuntu Server"**
2. Follow the installer:
   - Language: English
   - Keyboard: whatever you use
   - **Network**: Leave defaults (DHCP)
   - **Proxy**: Skip
   - **Mirror**: Leave default
   - **Storage**: Use entire disk (defaults)
   - **Your name**: `ccrun`
   - **Server name**: `ccrun-vm`
   - **Username**: `ccrun`
   - **Password**: `ccrun123` (or whatever you prefer — remember it!)
   - **Install OpenSSH server**: ✅ YES (important!)
   - **Featured snaps**: Skip, select nothing
3. Wait for installation to complete (~5-10 min)
4. **Remove the ISO**: When done, UTM will say "Reboot". Power off the VM first, go to VM settings in UTM, remove the CD/DVD drive or eject the ISO, then start the VM again.

## Step 4: First Boot & SSH Setup

After the VM boots, log in with your username/password, then run:

```bash
# Get the VM's IP address
ip addr show | grep inet

# Enable password-less sudo (optional but convenient)
sudo visudo
# Add this line at the end: ccrun ALL=(ALL) NOPASSWD:ALL
```

## Step 5: Install All Toolchains

Copy-paste this entire block into the VM terminal:

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Python
sudo apt install -y python3 python3-pip python3-venv

# Go (latest)
wget https://go.dev/dl/go1.23.4.linux-arm64.tar.gz
sudo rm -rf /usr/local/go
sudo tar -C /usr/local -xzf go1.23.4.linux-arm64.tar.gz
echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.bashrc

# Rust
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"

# C build tools
sudo apt install -y gcc make build-essential libcurl4-openssl-dev libjson-c-dev

# Benchmark tools
sudo apt install -y sysbench hyperfine time strace

# Container prerequisites
sudo apt install -y debootstrap

# Download Alpine mini rootfs for container testing
mkdir -p ~/alpine-rootfs
wget https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/aarch64/alpine-minirootfs-3.20.6-aarch64.tar.gz
sudo tar -xzf alpine-minirootfs-3.20.6-aarch64.tar.gz -C ~/alpine-rootfs
sudo touch ~/alpine-rootfs/ALPINE_FS_ROOT

source ~/.bashrc
```

## Step 6: Verify Everything Works

```bash
python3 --version    # Should show 3.12+
go version           # Should show 1.23+
rustc --version      # Should show 1.8x+
gcc --version        # Should show 13+
sysbench --version   # Should show 1.0+
ls ~/alpine-rootfs/  # Should show Alpine filesystem
```

## Step 7: Note Your VM's IP

```bash
ip addr show enp0s1 | grep "inet "
```

Then from your Mac terminal, verify SSH works:
```bash
ssh ccrun@<VM_IP>
```

**Once SSH works, tell me the VM's IP address and I'll deploy all the code!**
