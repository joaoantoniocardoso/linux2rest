# linux2rest CPU Accuracy Test Suite

This test suite compares CPU measurements from two linux2rest binaries against system ground truth, with support for:
- Concurrent request testing (cache-miss race condition detection)
- Request duration measurement
- Binary resource usage monitoring (CPU and memory)
- A vs B comparison plots

## Quick Start

### Local Testing

```bash
# Install dependencies
pip install -r requirements.txt

# Build two versions to compare
cargo build --release
cp target/release/linux2rest linux2rest_master

git checkout fix_sampling
cargo build --release
cp target/release/linux2rest linux2rest_fix_sampling
git checkout master

# Run the test
python3 cpu_accuracy_test.py \
    -a ./linux2rest_master \
    -b ./linux2rest_fix_sampling \
    -d 60 -c 5
```

### Testing on Raspberry Pi

#### Option 1: Build and Deploy Script

Use the `build-and-deploy.sh` script to cross-compile and deploy binaries:

```bash
# Deploy master branch
./build-and-deploy.sh \
    --target armv7-unknown-linux-gnueabihf \
    --host pi@192.168.1.100 \
    --branch origin/master \
    --name linux2rest_master

# Deploy fix_sampling branch
./build-and-deploy.sh \
    --target armv7-unknown-linux-gnueabihf \
    --host pi@192.168.1.100 \
    --branch fix_sampling \
    --name linux2rest_fix_sampling
```

Supported targets:
- `armv7-unknown-linux-gnueabihf` - Raspberry Pi 2/3 (32-bit)
- `aarch64-unknown-linux-gnu` - Raspberry Pi 3/4/5 (64-bit)
- `x86_64-unknown-linux-gnu` - Standard x86_64 Linux

#### Option 2: Docker

Build the multi-arch Docker image:

```bash
# Build for local architecture only
docker build -t linux2rest-test .

# Build and push multi-arch image
docker buildx build \
    --platform linux/amd64,linux/arm64,linux/arm/v7 \
    -t ghcr.io/bluerobotics/linux2rest-test \
    --push .
```

Run on Raspberry Pi:

```bash
# Pull the image
docker pull ghcr.io/bluerobotics/linux2rest-test

# Run with binaries mounted
docker run --rm \
    -v $(pwd):/binaries \
    -v $(pwd)/results:/app/results \
    ghcr.io/bluerobotics/linux2rest-test \
    -a /binaries/linux2rest_master \
    -b /binaries/linux2rest_fix_sampling \
    -d 60 -c 5
```

## CLI Options

```
usage: cpu_accuracy_test.py [-h] --binary-a BINARY_A --binary-b BINARY_B
                            [--port-a PORT_A] [--port-b PORT_B]
                            [--duration DURATION] [--concurrent CONCURRENT]
                            [--interval INTERVAL]
                            [--resource-interval RESOURCE_INTERVAL]
                            [--stress-cpus STRESS_CPUS] [--output OUTPUT]
                            [--warmup WARMUP] [--args-a ARGS_A]
                            [--args-b ARGS_B]

Options:
  --binary-a, -a        Path to first binary (e.g., master build)
  --binary-b, -b        Path to second binary (e.g., fix_sampling build)
  --port-a              Port for binary A (default: 6030)
  --port-b              Port for binary B (default: 6031)
  --duration, -d        Test duration in seconds (default: 60)
  --concurrent, -c      Number of concurrent requests per sample (default: 2)
  --interval, -i        Sample interval in seconds (default: 1.0)
  --resource-interval   Resource monitor interval in seconds (default: 0.1)
  --stress-cpus, -s     Number of stress CPU workers (default: 2)
  --output, -o          Output directory for results (default: ./results)
  --warmup, -w          Warmup time in seconds (default: 10)
  --args-a              Extra arguments for binary A (default: --disable-zenoh)
  --args-b              Extra arguments for binary B (default: none)
```

## Output Files

The test generates several output files in the results directory:

| File | Description |
|------|-------------|
| `cpu_test_<timestamp>.csv` | Raw API sample data |
| `cpu_test_<timestamp>_resources.csv` | Resource usage samples |
| `cpu_test_<timestamp>_full.json` | Complete data with all fields |
| `cpu_test_<timestamp>.png` | Visualization plots |
| `cpu_test_<timestamp>_report.txt` | Text summary report |

## Understanding the Results

### Plots

The test generates a 2x2 plot:

1. **CPU Measurements Over Time** (top-left): System CPU readings from both binaries vs ground truth (psutil)
2. **Request Duration Over Time** (top-right): Response time per API request
3. **Binary CPU Usage** (bottom-left): Process CPU consumption of each binary
4. **Binary Memory Usage** (bottom-right): Process memory consumption

### Metrics

- **CPU Measurements**: Compares what the API reports vs actual system CPU
- **Request Duration**: Measures API responsiveness (P50, P95, P99)
- **Resource Usage**: Monitors the binary's own CPU and memory footprint

### Known Issues to Detect

1. **First-sample 0% bug**: Initial CPU readings return 0% due to sysinfo library behavior
2. **Cache-miss race condition**: Concurrent requests returning different values
3. **High latency spikes**: P99 >> P95 indicates occasional slow responses

## Requirements

- Python 3.11+
- `stress` command (for CPU load generation)
- Dependencies: `aiohttp`, `pandas`, `matplotlib`, `psutil`

For cross-compilation:
- Rust with `cross` (`cargo install cross`)
- Docker (for cross compilation)

## Example Test Run

```bash
# Comprehensive test on Raspberry Pi
python3 cpu_accuracy_test.py \
    -a ./linux2rest_master \
    -b ./linux2rest_fix_sampling \
    --duration 120 \
    --concurrent 5 \
    --interval 1.0 \
    --resource-interval 0.1 \
    --stress-cpus 2 \
    --warmup 15 \
    --args-a "" \
    --args-b ""
```

This will:
- Run for 2 minutes
- Make 5 concurrent requests every second
- Monitor resource usage every 100ms
- Generate 2 CPU cores worth of stress load
- Allow 15 seconds warmup before measurement
