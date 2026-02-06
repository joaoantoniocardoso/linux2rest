#!/bin/bash
# Build linux2rest binaries and deploy to a remote machine for testing
#
# This script:
# 1. Cross-compiles linux2rest for the specified target architecture
# 2. Copies the binary to a remote machine via SSH
# 3. Optionally builds/pushes the Docker test image
#
# Requirements:
# - Rust toolchain with cross (cargo install cross)
# - Docker with buildx for multi-arch builds
# - SSH access to the target machine
#
# Usage:
#   ./build-and-deploy.sh --target armv7-unknown-linux-gnueabihf --host pi@192.168.1.100
#   ./build-and-deploy.sh --target aarch64-unknown-linux-gnu --host pi@rpi4.local --branch fix_sampling
#   ./build-and-deploy.sh --docker-only --push  # Only build and push Docker image

set -e

# Default values
TARGET=""
HOST=""
BRANCH=""
BINARY_NAME=""
REMOTE_DIR="~/linux2rest-test"
DOCKER_IMAGE="linux2rest-test"
DOCKER_REGISTRY=""
BUILD_DOCKER=false
PUSH_DOCKER=false
DOCKER_ONLY=false
FEATURES=""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

print_usage() {
    cat << EOF
Usage: $0 [OPTIONS]

Build and deploy linux2rest binaries for testing.

OPTIONS:
    --target TARGET       Rust target triple (e.g., armv7-unknown-linux-gnueabihf)
    --host USER@HOST      SSH destination (e.g., pi@192.168.1.100)
    --branch BRANCH       Git branch to build (optional, uses current if not specified)
    --name NAME           Name for the binary on remote (default: linux2rest_<branch>)
    --remote-dir DIR      Remote directory for binaries (default: ~/linux2rest-test)
    --features FEATURES   Cargo features to enable (default: auto-detect based on target)
    
    --docker              Also build the Docker test image
    --docker-only         Only build Docker image (skip binary compilation)
    --push                Push Docker image to registry
    --registry REGISTRY   Docker registry (e.g., ghcr.io/bluerobotics)
    --image NAME          Docker image name (default: linux2rest-test)
    
    -h, --help            Show this help message

EXAMPLES:
    # Build for ARM32 and deploy to Raspberry Pi 3
    $0 --target armv7-unknown-linux-gnueabihf --host pi@192.168.1.100

    # Build specific branch for ARM64
    $0 --target aarch64-unknown-linux-gnu --host pi@rpi4.local --branch fix_sampling

    # Build both master and fix_sampling branches
    $0 --target armv7-unknown-linux-gnueabihf --host pi@rpi.local --branch master --name linux2rest_master
    $0 --target armv7-unknown-linux-gnueabihf --host pi@rpi.local --branch fix_sampling --name linux2rest_fix_sampling

    # Build and push multi-arch Docker image
    $0 --docker-only --push --registry ghcr.io/bluerobotics

SUPPORTED TARGETS:
    armv7-unknown-linux-gnueabihf   Raspberry Pi 2/3 (32-bit)
    aarch64-unknown-linux-gnu       Raspberry Pi 3/4/5 (64-bit)
    x86_64-unknown-linux-gnu        Standard x86_64 Linux

EOF
}

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --target)
            TARGET="$2"
            shift 2
            ;;
        --host)
            HOST="$2"
            shift 2
            ;;
        --branch)
            BRANCH="$2"
            shift 2
            ;;
        --name)
            BINARY_NAME="$2"
            shift 2
            ;;
        --remote-dir)
            REMOTE_DIR="$2"
            shift 2
            ;;
        --features)
            FEATURES="$2"
            shift 2
            ;;
        --docker)
            BUILD_DOCKER=true
            shift
            ;;
        --docker-only)
            DOCKER_ONLY=true
            BUILD_DOCKER=true
            shift
            ;;
        --push)
            PUSH_DOCKER=true
            shift
            ;;
        --registry)
            DOCKER_REGISTRY="$2"
            shift 2
            ;;
        --image)
            DOCKER_IMAGE="$2"
            shift 2
            ;;
        -h|--help)
            print_usage
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            print_usage
            exit 1
            ;;
    esac
done

# Get project root (parent of tests directory)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Validate arguments
if [[ "$DOCKER_ONLY" == false ]]; then
    if [[ -z "$TARGET" ]]; then
        log_error "Target is required. Use --target <TARGET>"
        print_usage
        exit 1
    fi
    
    if [[ -z "$HOST" ]]; then
        log_error "Host is required. Use --host <USER@HOST>"
        print_usage
        exit 1
    fi
fi

# Auto-detect features based on target
if [[ -z "$FEATURES" ]]; then
    case "$TARGET" in
        armv7-unknown-linux-gnueabihf|aarch64-unknown-linux-gnu)
            FEATURES="raspberry"
            ;;
        *)
            FEATURES=""
            ;;
    esac
fi

# Build Docker image if requested
build_docker() {
    log_info "Building Docker image..."
    cd "$SCRIPT_DIR"
    
    # Determine platforms
    PLATFORMS="linux/amd64,linux/arm64,linux/arm/v7"
    
    # Determine full image name
    if [[ -n "$DOCKER_REGISTRY" ]]; then
        FULL_IMAGE="$DOCKER_REGISTRY/$DOCKER_IMAGE"
    else
        FULL_IMAGE="$DOCKER_IMAGE"
    fi
    
    # Build command
    BUILD_CMD="docker buildx build --platform $PLATFORMS -t $FULL_IMAGE"
    
    if [[ "$PUSH_DOCKER" == true ]]; then
        BUILD_CMD="$BUILD_CMD --push"
        log_info "Building and pushing to: $FULL_IMAGE"
    else
        BUILD_CMD="$BUILD_CMD --load"
        log_warn "Building locally (single platform). Use --push to push multi-arch."
        # For local builds, we can only build for the current platform
        BUILD_CMD="docker build -t $FULL_IMAGE"
    fi
    
    log_info "Running: $BUILD_CMD ."
    eval "$BUILD_CMD ."
    
    log_info "Docker image built: $FULL_IMAGE"
}

# Build binary
build_binary() {
    log_info "Building linux2rest for $TARGET..."
    cd "$PROJECT_ROOT"
    
    # Checkout branch if specified
    ORIGINAL_BRANCH=""
    if [[ -n "$BRANCH" ]]; then
        ORIGINAL_BRANCH=$(git rev-parse --abbrev-ref HEAD)
        log_info "Checking out branch: $BRANCH"
        git checkout "$BRANCH"
    fi
    
    # Build with cross
    CARGO_CMD="cross build --release --target=$TARGET"
    if [[ -n "$FEATURES" ]]; then
        CARGO_CMD="$CARGO_CMD --features=$FEATURES"
    fi
    
    log_info "Running: $CARGO_CMD"
    eval "$CARGO_CMD"
    
    # Determine binary name
    if [[ -z "$BINARY_NAME" ]]; then
        if [[ -n "$BRANCH" ]]; then
            BINARY_NAME="linux2rest_${BRANCH}"
        else
            BINARY_NAME="linux2rest"
        fi
    fi
    
    # Store the built binary path
    BUILT_BINARY="$PROJECT_ROOT/target/$TARGET/release/linux2rest"
    
    # Restore original branch
    if [[ -n "$ORIGINAL_BRANCH" ]]; then
        log_info "Restoring branch: $ORIGINAL_BRANCH"
        git checkout "$ORIGINAL_BRANCH"
    fi
}

# Deploy binary to remote
deploy_binary() {
    log_info "Deploying $BINARY_NAME to $HOST:$REMOTE_DIR..."
    
    # Create remote directory
    ssh "$HOST" "mkdir -p $REMOTE_DIR"
    
    # Copy binary
    scp "$BUILT_BINARY" "$HOST:$REMOTE_DIR/$BINARY_NAME"
    
    # Make executable
    ssh "$HOST" "chmod +x $REMOTE_DIR/$BINARY_NAME"
    
    log_info "Binary deployed: $HOST:$REMOTE_DIR/$BINARY_NAME"
}

# Deploy test files to remote
deploy_test_files() {
    log_info "Deploying test files to $HOST:$REMOTE_DIR..."
    
    # Copy test script and requirements
    scp "$SCRIPT_DIR/cpu_accuracy_test.py" "$HOST:$REMOTE_DIR/"
    scp "$SCRIPT_DIR/requirements.txt" "$HOST:$REMOTE_DIR/"
    
    log_info "Test files deployed"
}

# Main execution
main() {
    log_info "=============================================="
    log_info "linux2rest Build and Deploy Tool"
    log_info "=============================================="
    
    if [[ "$DOCKER_ONLY" == true ]]; then
        build_docker
        exit 0
    fi
    
    # Build the binary
    build_binary
    
    # Deploy to remote
    deploy_binary
    
    # Deploy test files
    deploy_test_files
    
    # Build Docker if requested
    if [[ "$BUILD_DOCKER" == true ]]; then
        build_docker
    fi
    
    log_info "=============================================="
    log_info "Deployment complete!"
    log_info ""
    log_info "To run tests on the remote machine:"
    log_info "  ssh $HOST"
    log_info "  cd $REMOTE_DIR"
    log_info ""
    log_info "  # Option 1: Run with Python directly"
    log_info "  pip install -r requirements.txt"
    log_info "  python3 cpu_accuracy_test.py -a ./linux2rest_master -b ./linux2rest_fix_sampling -d 60"
    log_info ""
    log_info "  # Option 2: Run with Docker"
    log_info "  docker run --rm -v \$(pwd):/binaries -v \$(pwd)/results:/app/results $DOCKER_IMAGE \\"
    log_info "      -a /binaries/linux2rest_master -b /binaries/linux2rest_fix_sampling -d 60"
    log_info "=============================================="
}

main
