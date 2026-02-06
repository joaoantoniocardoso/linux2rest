#!/bin/bash
set -e

PI_HOST="192.168.2.2"
PI_USER="pi"
PI_PASS="raspberry"
PI_DIR="~/linux2rest-test"
LOCAL_DIR="/home/joaoantoniocardoso/BlueRobotics/linux2rest"
TARGET="armv7-unknown-linux-gnueabihf"

echo "=============================================="
echo "Quick Test Script"
echo "=============================================="

# Step 1: Rebuild current state as Binary B
echo ""
echo "[1/4] Building current state (Binary B)..."
cd "$LOCAL_DIR"
cross build --release --target $TARGET 2>&1 | tail -5

NEW_SHA=$(sha256sum target/$TARGET/release/linux2rest | cut -d' ' -f1)
echo "  Binary B SHA: ${NEW_SHA:0:16}..."

# Step 2: Deploy Binary B to Pi
echo ""
echo "[2/4] Deploying Binary B to Raspberry Pi..."
sshpass -p "$PI_PASS" scp target/$TARGET/release/linux2rest $PI_USER@$PI_HOST:$PI_DIR/linux2rest_fix_sampling 2>/dev/null

# Verify SHA on Pi
REMOTE_SHA=$(sshpass -p "$PI_PASS" ssh $PI_USER@$PI_HOST "sha256sum $PI_DIR/linux2rest_fix_sampling" 2>/dev/null | cut -d' ' -f1)
if [ "$NEW_SHA" != "$REMOTE_SHA" ]; then
    echo "  ERROR: SHA mismatch!"
    exit 1
fi
echo "  SHA verified on Pi"

# Clear old results
sshpass -p "$PI_PASS" ssh $PI_USER@$PI_HOST "rm -rf $PI_DIR/results/* 2>/dev/null; mkdir -p $PI_DIR/results" 2>/dev/null

# Step 3: Run the test
echo ""
echo "[3/4] Running test (60 seconds)..."
sshpass -p "$PI_PASS" ssh $PI_USER@$PI_HOST "cd $PI_DIR && python3 cpu_accuracy_test.py \
    --binary-a $PI_DIR/linux2rest_master \
    --binary-b $PI_DIR/linux2rest_fix_sampling \
    --port-a 26030 --port-b 26031 \
    --duration 60 --concurrent 5 --interval 1.0 \
    --resource-interval 0.1 --stress-cpus 2 \
    --output results \
    --args-a '--log-settings netstat=30,platform=10,serial-ports=10,cpu=10,disk=30,info=10,memory=10,network=10,process=60,temperature=10,unix-time-seconds=10,usb=60' \
    --args-b '--log-settings netstat=30,platform=10,serial-ports=10,cpu=10,disk=30,info=10,memory=10,network=10,process=60,temperature=10,unix-time-seconds=10,usb=60' \
    --external-process mavlink-camera-manager" 2>/dev/null | tail -40

# Step 4: Get results and analyze
echo ""
echo "[4/4] Downloading and analyzing results..."
rm -rf "$LOCAL_DIR/tests/results/"* 2>/dev/null || true
sshpass -p "$PI_PASS" rsync -az $PI_USER@$PI_HOST:$PI_DIR/results/ "$LOCAL_DIR/tests/results/" 2>/dev/null

# Find the CSV file and analyze
CSV_FILE=$(ls -t "$LOCAL_DIR/tests/results/"cpu_test_*.csv 2>/dev/null | grep -v '_external\|_resources\|_self_reported' | head -1)

if [ -z "$CSV_FILE" ]; then
    echo "  ERROR: No CSV file found!"
    exit 1
fi

echo ""
echo "=============================================="
echo "RESULTS ANALYSIS"
echo "=============================================="

python3 << PYEOF
import pandas as pd
import glob

# Main CSV - System CPU measurements
df = pd.read_csv("$CSV_FILE")

print("--- SYSTEM CPU MEASUREMENTS ---")
print()
for source, label in [('binary_a', 'Binary A (origin/master)'), ('binary_b', 'Binary B (fix_sampling)')]:
    source_df = df[df['source'] == source]
    cpu_values = source_df['cpu_total'].dropna()
    
    zeros = (cpu_values == 0).sum()
    four_hundreds = (cpu_values >= 400).sum()
    total = len(cpu_values)
    mean = cpu_values.mean()
    std = cpu_values.std()
    
    print(f"{label}:")
    print(f"  Total samples: {total}")
    print(f"  Mean CPU:      {mean:.1f}% (±{std:.1f}%)")
    print(f"  Zero values:   {zeros} ({100*zeros/total:.1f}%)")
    print(f"  400% values:   {four_hundreds} ({100*four_hundreds/total:.1f}%)")
    print()

# External CSV - External process monitoring (mavlink-camera-manager)
ext_csv = "$CSV_FILE".replace('.csv', '_external.csv')
try:
    ext_df = pd.read_csv(ext_csv)
    
    print("--- EXTERNAL PROCESS (mavlink-camera-manager) ---")
    print()
    for source, label in [('psutil', 'Ground Truth (psutil)'), ('api_a', 'Binary A API'), ('api_b', 'Binary B API')]:
        source_df = ext_df[ext_df['source'] == source]
        cpu_values = source_df['cpu_percent'].dropna()
        
        if len(cpu_values) == 0:
            continue
        
        zeros = (cpu_values == 0).sum()
        four_hundreds = (cpu_values >= 400).sum()
        total = len(cpu_values)
        mean = cpu_values.mean()
        std = cpu_values.std()
        
        print(f"{label}:")
        print(f"  Total samples: {total}")
        print(f"  Mean CPU:      {mean:.1f}% (±{std:.1f}%)")
        print(f"  Zero values:   {zeros} ({100*zeros/total:.1f}%)")
        print(f"  400% values:   {four_hundreds} ({100*four_hundreds/total:.1f}%)")
        print()
except:
    pass

PYEOF

echo "Results saved to: $LOCAL_DIR/tests/results/"
