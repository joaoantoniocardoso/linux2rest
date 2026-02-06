#!/usr/bin/env python3
"""
CPU Measurement Accuracy Test for linux2rest

This test compares CPU measurements from two linux2rest binaries (A and B)
against ground truth from the system (using psutil/top).

The test uses concurrent requests to trigger cache-miss race conditions
that have been observed in production (BlueOS).

Usage:
    python cpu_accuracy_test.py --binary-a ./binary_a --binary-b ./binary_b --duration 60

Requirements:
    pip install aiohttp pandas matplotlib psutil
"""

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp
import pandas as pd
import psutil

# Optional: matplotlib for plotting (may not be available on headless systems)
try:
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False


@dataclass
class Config:
    """Test configuration"""
    binary_a: Path
    binary_b: Path
    port_a: int = 26030  # Avoid conflict with system linux2rest on 6030
    port_b: int = 26031  # Avoid conflict with system linux2rest on 6031
    args_a: list[str] = field(default_factory=list)
    args_b: list[str] = field(default_factory=list)
    duration: int = 60  # seconds
    sample_interval: float = 1.0  # seconds between API samples
    top_interval: float = 1.0  # seconds between ground truth samples
    resource_interval: float = 0.1  # seconds between resource monitoring (100ms)
    concurrent_requests: int = 2  # number of concurrent requests per sample
    output_dir: Path = Path("./results")
    stress_cpus: int = 2  # number of stress workers to run
    warmup_time: int = 0  # no warmup - capture entire runtime including first samples
    external_process: Optional[str] = None  # Optional external process to monitor (e.g., "mavlink-camera-manager")


@dataclass
class Sample:
    """A single measurement sample"""
    timestamp: float
    source: str  # 'binary_a', 'binary_b', 'ground_truth'
    request_id: int  # for concurrent request tracking
    cpu_total: float
    cpu_per_core: list[float]
    duration_ms: float = 0.0  # request duration in milliseconds
    raw_response: Optional[dict] = None
    error: Optional[str] = None


@dataclass
class ResourceSample:
    """Resource usage sample for a binary (external measurement via psutil)"""
    timestamp: float
    source: str  # 'binary_a', 'binary_b'
    cpu_percent: float
    memory_mb: float
    memory_percent: float


@dataclass
class SelfReportedSample:
    """Self-reported resource usage from the binary's /system/process endpoint"""
    timestamp: float
    source: str  # 'binary_a', 'binary_b'
    cpu_percent: float
    memory_kb: float
    error: Optional[str] = None


@dataclass
class ExternalProcessSample:
    """Sample for external process monitoring (e.g., mavlink-camera-manager)"""
    timestamp: float
    source: str  # 'api_a', 'api_b', 'psutil'
    process_name: str
    cpu_percent: float
    memory_kb: float
    error: Optional[str] = None


@dataclass
class TestResults:
    """Container for all test results"""
    config: Config
    samples: list[Sample] = field(default_factory=list)
    resource_samples: list[ResourceSample] = field(default_factory=list)
    self_reported_samples: list[SelfReportedSample] = field(default_factory=list)
    external_process_samples: list[ExternalProcessSample] = field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0
    stress_pids: list[int] = field(default_factory=list)
    binary_a_pid: Optional[int] = None
    binary_b_pid: Optional[int] = None
    external_process_pid: Optional[int] = None


class CPUAccuracyTest:
    def __init__(self, config: Config):
        self.config = config
        self.results = TestResults(config=config)
        self._stop_event = asyncio.Event()
        self._processes: list[subprocess.Popen] = []
        
    async def run(self):
        """Run the complete test"""
        print("=" * 60)
        print("CPU Measurement Accuracy Test")
        print("=" * 60)
        print(f"Binary A: {self.config.binary_a}")
        print(f"Binary B: {self.config.binary_b}")
        print(f"Duration: {self.config.duration}s")
        print(f"Sample interval: {self.config.sample_interval}s")
        print(f"Resource monitor interval: {self.config.resource_interval}s")
        print(f"Concurrent requests: {self.config.concurrent_requests}")
        print(f"Stress CPUs: {self.config.stress_cpus}")
        if self.config.external_process:
            print(f"External process monitoring: {self.config.external_process}")
        print("=" * 60)
        
        try:
            # Setup
            self.config.output_dir.mkdir(parents=True, exist_ok=True)
            
            # Start stress load
            print("\n[1/6] Starting CPU stress load...")
            self._start_stress()
            
            # Start binaries
            print("\n[2/6] Starting binary A...")
            self._start_binary_a()
            
            print("\n[3/6] Starting binary B...")
            self._start_binary_b()
            
            # Verify servers are responding (no warmup - capture entire runtime)
            print("\n[4/6] Verifying servers...")
            await self._verify_servers()
            
            if self.config.warmup_time > 0:
                print(f"  Additional warmup: {self.config.warmup_time}s")
                await asyncio.sleep(self.config.warmup_time)
            
            # Run measurements
            print(f"\n[5/6] Collecting measurements for {self.config.duration}s...")
            self.results.start_time = time.time()
            await self._collect_measurements()
            self.results.end_time = time.time()
            
            # Generate report
            print("\n[6/6] Generating report...")
            self._generate_report()
            
        finally:
            # Cleanup
            print("\nCleaning up...")
            self._cleanup()
            
    def _start_stress(self):
        """Start stress workers"""
        if self.config.stress_cpus <= 0:
            print("  Stress disabled (--stress-cpus 0)")
            return
            
        try:
            proc = subprocess.Popen(
                ["stress", "--cpu", str(self.config.stress_cpus), 
                 "--timeout", str(self.config.duration + self.config.warmup_time + 30)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            self._processes.append(proc)
            time.sleep(1)
            
            # Find stress worker PIDs
            for p in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    if p.info['name'] == 'stress':
                        self.results.stress_pids.append(p.info['pid'])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                    
            print(f"  Started {len(self.results.stress_pids)} stress workers")
        except FileNotFoundError:
            print("  WARNING: 'stress' command not found. Running without load.")
            
    def _start_binary_a(self):
        """Start binary A"""
        env = os.environ.copy()
        cmd = [str(self.config.binary_a), "--port", str(self.config.port_a)]
        cmd.extend(self.config.args_a)
        
        # Create log file for debugging
        log_file = open("/tmp/binary_a.log", "w")
        
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True  # Detach from parent process group
        )
        self._processes.append(proc)
        self.results.binary_a_pid = proc.pid
        print(f"  Command: {' '.join(cmd)}")
        print(f"  Started on port {self.config.port_a} (PID: {proc.pid})")
        time.sleep(1)  # Brief startup time, verify_servers will retry
        
    def _start_binary_b(self):
        """Start binary B"""
        env = os.environ.copy()
        cmd = [str(self.config.binary_b), "--port", str(self.config.port_b)]
        cmd.extend(self.config.args_b)
        
        # Create log file for debugging
        log_file = open("/tmp/binary_b.log", "w")
        
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True  # Detach from parent process group
        )
            
        self._processes.append(proc)
        self.results.binary_b_pid = proc.pid
        print(f"  Command: {' '.join(cmd)}")
        print(f"  Started on port {self.config.port_b} (PID: {proc.pid})")
        time.sleep(1)  # Brief startup time, verify_servers will retry
        
    async def _verify_servers(self):
        """Verify both servers are responding (with retries for slow startup)"""
        max_retries = 20
        retry_delay = 0.5
        
        async with aiohttp.ClientSession() as session:
            for name, port, log_file in [
                ("A", self.config.port_a, "/tmp/binary_a.log"),
                ("B", self.config.port_b, "/tmp/binary_b.log")
            ]:
                for attempt in range(max_retries):
                    try:
                        async with session.get(f"http://localhost:{port}/system/info", timeout=aiohttp.ClientTimeout(total=2)) as resp:
                            if resp.status == 200:
                                print(f"  Binary {name} responding OK (attempt {attempt + 1})")
                                break
                            else:
                                print(f"  WARNING: Binary {name} returned status {resp.status}")
                    except Exception as e:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(retry_delay)
                        else:
                            print(f"  ERROR: Binary {name} not responding after {max_retries} attempts")
                            # Show log file for debugging
                            try:
                                with open(log_file, "r") as f:
                                    log_content = f.read()
                                    if log_content:
                                        print(f"  Binary {name} log output:")
                                        for line in log_content.split('\n')[-20:]:
                                            print(f"    {line}")
                            except:
                                pass
                            raise RuntimeError(f"Binary {name} failed to start: {e}")
                    
    async def _collect_measurements(self):
        """Collect measurements from all sources"""
        tasks = [
            self._collect_api_samples("binary_a", self.config.port_a),
            self._collect_api_samples("binary_b", self.config.port_b),
            self._collect_ground_truth(),
            self._monitor_resources(),
            self._collect_self_reported("binary_a", self.config.port_a, self.results.binary_a_pid),
            self._collect_self_reported("binary_b", self.config.port_b, self.results.binary_b_pid),
        ]
        
        # Add external process monitoring if configured
        if self.config.external_process:
            tasks.extend([
                self._monitor_external_process_psutil(),
                self._monitor_external_process_api("api_a", self.config.port_a),
                self._monitor_external_process_api("api_b", self.config.port_b),
            ])
        
        # Run for specified duration
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=self.config.duration + 5
            )
        except asyncio.TimeoutError:
            pass
        
        self._stop_event.set()
        print(f"\n  Collected {len(self.results.samples)} API samples")
        print(f"  Collected {len(self.results.resource_samples)} resource samples")
        print(f"  Collected {len(self.results.self_reported_samples)} self-reported samples")
        if self.config.external_process:
            print(f"  Collected {len(self.results.external_process_samples)} external process samples")
        
    async def _collect_api_samples(self, source: str, port: int):
        """Collect samples from an API endpoint with concurrent requests"""
        url = f"http://localhost:{port}/system/cpu"
        end_time = time.time() + self.config.duration
        
        async with aiohttp.ClientSession() as session:
            while time.time() < end_time and not self._stop_event.is_set():
                # Make concurrent requests to trigger cache-miss race condition
                tasks = []
                for req_id in range(self.config.concurrent_requests):
                    tasks.append(self._fetch_cpu(session, url, source, req_id))
                    
                await asyncio.gather(*tasks)
                await asyncio.sleep(self.config.sample_interval)
                
    async def _fetch_cpu(self, session: aiohttp.ClientSession, url: str, 
                         source: str, request_id: int):
        """Fetch CPU data from API"""
        start_time = time.time()
        timestamp = start_time
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                end_time = time.time()
                duration_ms = (end_time - start_time) * 1000
                
                if resp.status == 200:
                    data = await resp.json()
                    cpu_per_core = [cpu.get('usage', 0) for cpu in data]
                    cpu_total = sum(cpu_per_core)
                    
                    sample = Sample(
                        timestamp=timestamp,
                        source=source,
                        request_id=request_id,
                        cpu_total=cpu_total,
                        cpu_per_core=cpu_per_core,
                        duration_ms=duration_ms,
                        raw_response=data
                    )
                else:
                    sample = Sample(
                        timestamp=timestamp,
                        source=source,
                        request_id=request_id,
                        cpu_total=0,
                        cpu_per_core=[],
                        duration_ms=duration_ms,
                        error=f"HTTP {resp.status}"
                    )
        except Exception as e:
            end_time = time.time()
            duration_ms = (end_time - start_time) * 1000
            sample = Sample(
                timestamp=timestamp,
                source=source,
                request_id=request_id,
                cpu_total=0,
                cpu_per_core=[],
                duration_ms=duration_ms,
                error=str(e)
            )
            
        self.results.samples.append(sample)
        
    async def _collect_ground_truth(self):
        """Collect ground truth CPU measurements using psutil"""
        end_time = time.time() + self.config.duration
        
        # Initialize CPU percent (first call returns 0)
        psutil.cpu_percent(percpu=True)
        await asyncio.sleep(0.5)
        
        while time.time() < end_time and not self._stop_event.is_set():
            timestamp = time.time()
            
            try:
                cpu_per_core = psutil.cpu_percent(percpu=True)
                cpu_total = sum(cpu_per_core)
                
                sample = Sample(
                    timestamp=timestamp,
                    source="ground_truth",
                    request_id=0,
                    cpu_total=cpu_total,
                    cpu_per_core=cpu_per_core
                )
            except Exception as e:
                sample = Sample(
                    timestamp=timestamp,
                    source="ground_truth",
                    request_id=0,
                    cpu_total=0,
                    cpu_per_core=[],
                    error=str(e)
                )
                
            self.results.samples.append(sample)
            await asyncio.sleep(self.config.top_interval)
            
    async def _monitor_resources(self):
        """Monitor resource usage (CPU and memory) for both binaries"""
        end_time = time.time() + self.config.duration
        
        # Track processes with their last CPU times for manual calculation
        proc_a_handle = None
        proc_b_handle = None
        last_cpu_times_a = None
        last_cpu_times_b = None
        last_time = time.time()
        
        # Get initial process handles
        def get_process_handle(pid):
            if pid is None:
                return None
            try:
                proc = psutil.Process(pid)
                # Verify it's still running
                if proc.is_running():
                    return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            return None
        
        def get_cpu_and_memory(proc, last_cpu_times, elapsed):
            """Get CPU percentage and memory for a process"""
            if proc is None:
                return None, None, None, None
            try:
                # Get current CPU times
                cpu_times = proc.cpu_times()
                current_total = cpu_times.user + cpu_times.system
                
                # Calculate CPU percentage
                if last_cpu_times is not None and elapsed > 0:
                    cpu_delta = current_total - last_cpu_times
                    cpu_pct = (cpu_delta / elapsed) * 100
                else:
                    cpu_pct = 0.0
                
                # Get memory info
                mem_info = proc.memory_info()
                mem_mb = mem_info.rss / (1024 * 1024)
                mem_pct = proc.memory_percent()
                
                return cpu_pct, mem_mb, mem_pct, current_total
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return None, None, None, last_cpu_times
        
        # Initialize handles
        proc_a_handle = get_process_handle(self.results.binary_a_pid)
        proc_b_handle = get_process_handle(self.results.binary_b_pid)
        
        # Get initial CPU times
        if proc_a_handle:
            try:
                cpu_times = proc_a_handle.cpu_times()
                last_cpu_times_a = cpu_times.user + cpu_times.system
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if proc_b_handle:
            try:
                cpu_times = proc_b_handle.cpu_times()
                last_cpu_times_b = cpu_times.user + cpu_times.system
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        
        await asyncio.sleep(self.config.resource_interval)
        
        while time.time() < end_time and not self._stop_event.is_set():
            timestamp = time.time()
            elapsed = timestamp - last_time
            last_time = timestamp
            
            # Refresh process handles if needed
            if proc_a_handle is None or not proc_a_handle.is_running():
                proc_a_handle = get_process_handle(self.results.binary_a_pid)
                last_cpu_times_a = None
            if proc_b_handle is None or not proc_b_handle.is_running():
                proc_b_handle = get_process_handle(self.results.binary_b_pid)
                last_cpu_times_b = None
            
            # Sample binary A
            cpu_pct, mem_mb, mem_pct, new_cpu_times = get_cpu_and_memory(
                proc_a_handle, last_cpu_times_a, elapsed
            )
            if cpu_pct is not None:
                last_cpu_times_a = new_cpu_times
                self.results.resource_samples.append(ResourceSample(
                    timestamp=timestamp,
                    source="binary_a",
                    cpu_percent=cpu_pct,
                    memory_mb=mem_mb,
                    memory_percent=mem_pct
                ))
            
            # Sample binary B
            cpu_pct, mem_mb, mem_pct, new_cpu_times = get_cpu_and_memory(
                proc_b_handle, last_cpu_times_b, elapsed
            )
            if cpu_pct is not None:
                last_cpu_times_b = new_cpu_times
                self.results.resource_samples.append(ResourceSample(
                    timestamp=timestamp,
                    source="binary_b",
                    cpu_percent=cpu_pct,
                    memory_mb=mem_mb,
                    memory_percent=mem_pct
                ))
                    
            await asyncio.sleep(self.config.resource_interval)
    
    async def _collect_self_reported(self, source: str, port: int, pid: int):
        """Collect self-reported CPU/memory from binary's /system/process endpoint"""
        url = f"http://localhost:{port}/system/process"
        end_time = time.time() + self.config.duration
        
        async with aiohttp.ClientSession() as session:
            while time.time() < end_time and not self._stop_event.is_set():
                timestamp = time.time()
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            # Find our own process by PID
                            for proc in data:
                                if proc.get('pid') == pid:
                                    self.results.self_reported_samples.append(SelfReportedSample(
                                        timestamp=timestamp,
                                        source=source,
                                        cpu_percent=proc.get('cpu_usage', 0.0),
                                        memory_kb=proc.get('used_memory_kB', 0)
                                    ))
                                    break
                            else:
                                # Process not found in list - might be named differently
                                # Try to find by name containing 'linux2rest'
                                for proc in data:
                                    if 'linux2rest' in proc.get('name', '').lower():
                                        self.results.self_reported_samples.append(SelfReportedSample(
                                            timestamp=timestamp,
                                            source=source,
                                            cpu_percent=proc.get('cpu_usage', 0.0),
                                            memory_kb=proc.get('used_memory_kB', 0)
                                        ))
                                        break
                except Exception as e:
                    self.results.self_reported_samples.append(SelfReportedSample(
                        timestamp=timestamp,
                        source=source,
                        cpu_percent=0.0,
                        memory_kb=0,
                        error=str(e)
                    ))
                
                await asyncio.sleep(self.config.sample_interval)
    
    async def _monitor_external_process_psutil(self):
        """Monitor external process (e.g., mavlink-camera-manager) using psutil as ground truth"""
        process_name = self.config.external_process
        if not process_name:
            return
            
        end_time = time.time() + self.config.duration
        
        # Find the process - skip bash/sh wrappers, find the actual binary
        proc = None
        candidates = []
        for p in psutil.process_iter(['pid', 'name', 'cmdline', 'exe']):
            try:
                name = p.info.get('name', '')
                cmdline = ' '.join(p.info.get('cmdline') or [])
                exe = p.info.get('exe', '') or ''
                
                # Skip if process_name is not in name, cmdline, or exe
                if not (process_name.lower() in name.lower() or 
                        process_name.lower() in cmdline.lower() or
                        process_name.lower() in exe.lower()):
                    continue
                
                # Skip bash/sh wrappers
                if name.lower() in ['bash', 'sh', 'dash', 'zsh']:
                    continue
                
                # Prefer processes where name exactly matches
                if process_name.lower() == name.lower():
                    proc = p
                    break
                
                # Or where the executable contains the process name
                if process_name.lower() in exe.lower():
                    proc = p
                    break
                    
                candidates.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        
        # Use first candidate if no exact match
        if not proc and candidates:
            proc = candidates[0]
        
        if proc:
            self.results.external_process_pid = proc.pid
            try:
                mem_info = proc.memory_info()
                print(f"  Found external process: {proc.name()} (PID: {proc.pid}, Memory: {mem_info.rss / 1024 / 1024:.1f} MB)")
            except:
                print(f"  Found external process: PID {proc.pid}")
        else:
            print(f"  WARNING: External process '{process_name}' not found")
            return
        
        # Initialize CPU measurement
        last_cpu_times = None
        last_time = time.time()
        
        try:
            cpu_times = proc.cpu_times()
            last_cpu_times = cpu_times.user + cpu_times.system
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return
        
        await asyncio.sleep(self.config.sample_interval)
        
        while time.time() < end_time and not self._stop_event.is_set():
            timestamp = time.time()
            elapsed = timestamp - last_time
            last_time = timestamp
            
            try:
                # Refresh process handle if needed
                if not proc.is_running():
                    proc = psutil.Process(self.results.external_process_pid)
                
                cpu_times = proc.cpu_times()
                current_total = cpu_times.user + cpu_times.system
                
                if last_cpu_times is not None and elapsed > 0:
                    cpu_delta = current_total - last_cpu_times
                    cpu_pct = (cpu_delta / elapsed) * 100
                else:
                    cpu_pct = 0.0
                
                last_cpu_times = current_total
                
                mem_info = proc.memory_info()
                mem_kb = mem_info.rss / 1024
                
                self.results.external_process_samples.append(ExternalProcessSample(
                    timestamp=timestamp,
                    source="psutil",
                    process_name=process_name,
                    cpu_percent=cpu_pct,
                    memory_kb=mem_kb
                ))
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                self.results.external_process_samples.append(ExternalProcessSample(
                    timestamp=timestamp,
                    source="psutil",
                    process_name=process_name,
                    cpu_percent=0.0,
                    memory_kb=0,
                    error=str(e)
                ))
            
            await asyncio.sleep(self.config.sample_interval)
    
    async def _monitor_external_process_api(self, source: str, port: int):
        """Monitor external process via linux2rest API /system/process endpoint"""
        process_name = self.config.external_process
        if not process_name:
            return
        
        # Wait for psutil to find the PID first
        while self.results.external_process_pid is None and not self._stop_event.is_set():
            await asyncio.sleep(0.1)
        
        if self.results.external_process_pid is None:
            return
            
        target_pid = self.results.external_process_pid
        url = f"http://localhost:{port}/system/process"
        end_time = time.time() + self.config.duration
        
        async with aiohttp.ClientSession() as session:
            while time.time() < end_time and not self._stop_event.is_set():
                timestamp = time.time()
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            # Find the external process by PID (matching psutil's finding)
                            for proc in data:
                                if proc.get('pid') == target_pid:
                                    self.results.external_process_samples.append(ExternalProcessSample(
                                        timestamp=timestamp,
                                        source=source,
                                        process_name=process_name,
                                        cpu_percent=proc.get('cpu_usage', 0.0),
                                        memory_kb=proc.get('used_memory_kB', 0)
                                    ))
                                    break
                            else:
                                # PID not found in API response
                                self.results.external_process_samples.append(ExternalProcessSample(
                                    timestamp=timestamp,
                                    source=source,
                                    process_name=process_name,
                                    cpu_percent=0.0,
                                    memory_kb=0,
                                    error=f"PID {target_pid} not found in API response"
                                ))
                except Exception as e:
                    self.results.external_process_samples.append(ExternalProcessSample(
                        timestamp=timestamp,
                        source=source,
                        process_name=process_name,
                        cpu_percent=0.0,
                        memory_kb=0,
                        error=str(e)
                    ))
                
                await asyncio.sleep(self.config.sample_interval)
            
    def _generate_report(self):
        """Generate test report and save results"""
        # Convert API samples to DataFrame
        df = pd.DataFrame([
            {
                'timestamp': s.timestamp,
                'relative_time': s.timestamp - self.results.start_time,
                'source': s.source,
                'request_id': s.request_id,
                'cpu_total': s.cpu_total,
                'duration_ms': s.duration_ms,
                'error': s.error
            }
            for s in self.results.samples
        ])
        
        # Convert resource samples to DataFrame
        resource_df = pd.DataFrame([
            {
                'timestamp': s.timestamp,
                'relative_time': s.timestamp - self.results.start_time,
                'source': s.source,
                'cpu_percent': s.cpu_percent,
                'memory_mb': s.memory_mb,
                'memory_percent': s.memory_percent
            }
            for s in self.results.resource_samples
        ])
        
        # Convert self-reported samples to DataFrame
        self_reported_df = pd.DataFrame([
            {
                'timestamp': s.timestamp,
                'relative_time': s.timestamp - self.results.start_time,
                'source': s.source,
                'cpu_percent': s.cpu_percent,
                'memory_kb': s.memory_kb,
                'error': s.error
            }
            for s in self.results.self_reported_samples
        ]) if self.results.self_reported_samples else pd.DataFrame()
        
        # Convert external process samples to DataFrame
        external_df = pd.DataFrame([
            {
                'timestamp': s.timestamp,
                'relative_time': s.timestamp - self.results.start_time,
                'source': s.source,
                'process_name': s.process_name,
                'cpu_percent': s.cpu_percent,
                'memory_kb': s.memory_kb,
                'error': s.error
            }
            for s in self.results.external_process_samples
        ]) if self.results.external_process_samples else pd.DataFrame()
        
        # Save raw data
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = self.config.output_dir / f"cpu_test_{timestamp_str}.csv"
        df.to_csv(csv_path, index=False)
        print(f"  Raw data saved to: {csv_path}")
        
        # Save resource data
        resource_csv_path = self.config.output_dir / f"cpu_test_{timestamp_str}_resources.csv"
        resource_df.to_csv(resource_csv_path, index=False)
        print(f"  Resource data saved to: {resource_csv_path}")
        
        # Save full responses as JSON
        json_path = self.config.output_dir / f"cpu_test_{timestamp_str}_full.json"
        with open(json_path, 'w') as f:
            json.dump({
                'samples': [
                    {
                        'timestamp': s.timestamp,
                        'source': s.source,
                        'request_id': s.request_id,
                        'cpu_total': s.cpu_total,
                        'cpu_per_core': s.cpu_per_core,
                        'duration_ms': s.duration_ms,
                        'error': s.error
                    }
                    for s in self.results.samples
                ],
                'resources': [
                    {
                        'timestamp': s.timestamp,
                        'source': s.source,
                        'cpu_percent': s.cpu_percent,
                        'memory_mb': s.memory_mb,
                        'memory_percent': s.memory_percent
                    }
                    for s in self.results.resource_samples
                ],
                'self_reported': [
                    {
                        'timestamp': s.timestamp,
                        'source': s.source,
                        'cpu_percent': s.cpu_percent,
                        'memory_kb': s.memory_kb,
                        'error': s.error
                    }
                    for s in self.results.self_reported_samples
                ],
                'external_process': [
                    {
                        'timestamp': s.timestamp,
                        'source': s.source,
                        'process_name': s.process_name,
                        'cpu_percent': s.cpu_percent,
                        'memory_kb': s.memory_kb,
                        'error': s.error
                    }
                    for s in self.results.external_process_samples
                ]
            }, f, indent=2)
        print(f"  Full data saved to: {json_path}")
        
        # Save self-reported data
        if not self_reported_df.empty:
            self_reported_csv_path = self.config.output_dir / f"cpu_test_{timestamp_str}_self_reported.csv"
            self_reported_df.to_csv(self_reported_csv_path, index=False)
            print(f"  Self-reported data saved to: {self_reported_csv_path}")
        
        # Save external process data
        if not external_df.empty:
            external_csv_path = self.config.output_dir / f"cpu_test_{timestamp_str}_external.csv"
            external_df.to_csv(external_csv_path, index=False)
            print(f"  External process data saved to: {external_csv_path}")
        
        # Generate statistics
        self._print_statistics(df, resource_df, self_reported_df, external_df)
        
        # Generate plot if matplotlib available
        if HAS_MATPLOTLIB:
            plot_path = self.config.output_dir / f"cpu_test_{timestamp_str}.png"
            self._generate_plot(df, resource_df, self_reported_df, plot_path)
            print(f"  Plot saved to: {plot_path}")
            
            # Generate external process plot if data available
            if not external_df.empty:
                external_plot_path = self.config.output_dir / f"cpu_test_{timestamp_str}_external.png"
                self._generate_external_plot(external_df, external_plot_path)
                print(f"  External process plot saved to: {external_plot_path}")
            
        # Save report
        report_path = self.config.output_dir / f"cpu_test_{timestamp_str}_report.txt"
        self._save_text_report(df, resource_df, self_reported_df, external_df, report_path)
        print(f"  Report saved to: {report_path}")
        
    def _print_statistics(self, df: pd.DataFrame, resource_df: pd.DataFrame, self_reported_df: pd.DataFrame, external_df: pd.DataFrame):
        """Print statistics for each source"""
        print("\n" + "=" * 60)
        print("RESULTS SUMMARY")
        print("=" * 60)
        
        expected_cpu = self.config.stress_cpus * 100
        print(f"\nExpected CPU (from {self.config.stress_cpus} stress workers): ~{expected_cpu}%")
        
        # CPU measurement statistics
        print("\n" + "-" * 60)
        print("CPU MEASUREMENTS")
        print("-" * 60)
        
        for source in ['ground_truth', 'binary_a', 'binary_b']:
            source_df = df[df['source'] == source]
            if len(source_df) == 0:
                continue
                
            valid_df = source_df[source_df['error'].isna()]
            errors = source_df[source_df['error'].notna()]
            
            # Find anomalies (0% or >150% of expected)
            anomalies = valid_df[
                (valid_df['cpu_total'] < 10) | 
                (valid_df['cpu_total'] > expected_cpu * 1.5)
            ]
            
            print(f"\n{source.upper()}:")
            print(f"  Samples: {len(source_df)} ({len(errors)} errors)")
            if len(valid_df) > 0:
                print(f"  CPU Total - Mean: {valid_df['cpu_total'].mean():.1f}%")
                print(f"  CPU Total - Std:  {valid_df['cpu_total'].std():.1f}%")
                print(f"  CPU Total - Min:  {valid_df['cpu_total'].min():.1f}%")
                print(f"  CPU Total - Max:  {valid_df['cpu_total'].max():.1f}%")
                print(f"  Anomalies (0% or >150% expected): {len(anomalies)}")
                
        # Request duration statistics
        print("\n" + "-" * 60)
        print("REQUEST DURATION")
        print("-" * 60)
        
        for source in ['binary_a', 'binary_b']:
            source_df = df[(df['source'] == source) & (df['error'].isna())]
            if len(source_df) == 0:
                continue
                
            print(f"\n{source.upper()}:")
            print(f"  Mean:   {source_df['duration_ms'].mean():.2f} ms")
            print(f"  Std:    {source_df['duration_ms'].std():.2f} ms")
            print(f"  Min:    {source_df['duration_ms'].min():.2f} ms")
            print(f"  Max:    {source_df['duration_ms'].max():.2f} ms")
            print(f"  P50:    {source_df['duration_ms'].quantile(0.50):.2f} ms")
            print(f"  P95:    {source_df['duration_ms'].quantile(0.95):.2f} ms")
            print(f"  P99:    {source_df['duration_ms'].quantile(0.99):.2f} ms")
            
        # Resource usage statistics
        print("\n" + "-" * 60)
        print("BINARY RESOURCE USAGE")
        print("-" * 60)
        
        for source in ['binary_a', 'binary_b']:
            source_df = resource_df[resource_df['source'] == source]
            if len(source_df) == 0:
                continue
                
            print(f"\n{source.upper()}:")
            print(f"  Samples: {len(source_df)}")
            print(f"  CPU - Mean: {source_df['cpu_percent'].mean():.1f}%")
            print(f"  CPU - Max:  {source_df['cpu_percent'].max():.1f}%")
            print(f"  Memory - Mean: {source_df['memory_mb'].mean():.1f} MB")
            print(f"  Memory - Max:  {source_df['memory_mb'].max():.1f} MB")
        
        # Self-reported resource usage statistics
        if not self_reported_df.empty:
            print("\n" + "-" * 60)
            print("SELF-REPORTED RESOURCE USAGE (from /system/process)")
            print("-" * 60)
            
            for source in ['binary_a', 'binary_b']:
                source_df = self_reported_df[(self_reported_df['source'] == source) & (self_reported_df['error'].isna())]
                if len(source_df) == 0:
                    continue
                    
                print(f"\n{source.upper()}:")
                print(f"  Samples: {len(source_df)}")
                print(f"  CPU - Mean: {source_df['cpu_percent'].mean():.1f}%")
                print(f"  CPU - Max:  {source_df['cpu_percent'].max():.1f}%")
                print(f"  CPU - Min:  {source_df['cpu_percent'].min():.1f}%")
                print(f"  Memory - Mean: {source_df['memory_kb'].mean() / 1024:.1f} MB")
                print(f"  Memory - Max:  {source_df['memory_kb'].max() / 1024:.1f} MB")
        
        # External process statistics
        if not external_df.empty:
            print("\n" + "-" * 60)
            print(f"EXTERNAL PROCESS: {self.config.external_process}")
            print("-" * 60)
            
            for source in ['psutil', 'api_a', 'api_b']:
                source_df = external_df[(external_df['source'] == source) & (external_df['error'].isna())]
                if len(source_df) == 0:
                    continue
                    
                source_label = {
                    'psutil': 'GROUND TRUTH (psutil)',
                    'api_a': 'BINARY A API',
                    'api_b': 'BINARY B API'
                }.get(source, source.upper())
                
                print(f"\n{source_label}:")
                print(f"  Samples: {len(source_df)}")
                print(f"  CPU - Mean: {source_df['cpu_percent'].mean():.1f}%")
                print(f"  CPU - Max:  {source_df['cpu_percent'].max():.1f}%")
                print(f"  CPU - Min:  {source_df['cpu_percent'].min():.1f}%")
                print(f"  Memory - Mean: {source_df['memory_kb'].mean() / 1024:.1f} MB")
                print(f"  Memory - Max:  {source_df['memory_kb'].max() / 1024:.1f} MB")
                
    def _generate_plot(self, df: pd.DataFrame, resource_df: pd.DataFrame, self_reported_df: pd.DataFrame, path: Path):
        """Generate visualization plot with A vs B comparisons"""
        fig, axes = plt.subplots(2, 2, figsize=(16, 12))
        
        colors = {
            'ground_truth': 'green',
            'binary_a': 'blue', 
            'binary_b': 'red'
        }
        
        # Plot 1: CPU Measurements Over Time (top-left)
        ax1 = axes[0, 0]
        for source in ['ground_truth', 'binary_a', 'binary_b']:
            source_df = df[(df['source'] == source) & (df['error'].isna())]
            if len(source_df) > 0:
                ax1.scatter(
                    source_df['relative_time'], 
                    source_df['cpu_total'],
                    c=colors.get(source, 'gray'),
                    label=source,
                    alpha=0.6,
                    s=20
                )
                
        ax1.axhline(y=self.config.stress_cpus * 100, color='black', 
                    linestyle='--', label=f'Expected ({self.config.stress_cpus}x100%)')
        ax1.set_xlabel('Time (s)')
        ax1.set_ylabel('Total CPU %')
        ax1.set_title('CPU Measurements Over Time')
        ax1.legend()
        ax1.grid(True, alpha=0.3)
        
        # Plot 2: Request Duration Over Time (top-right)
        ax2 = axes[0, 1]
        for source in ['binary_a', 'binary_b']:
            source_df = df[(df['source'] == source) & (df['error'].isna())]
            if len(source_df) > 0:
                ax2.scatter(
                    source_df['relative_time'], 
                    source_df['duration_ms'],
                    c=colors.get(source, 'gray'),
                    label=source,
                    alpha=0.6,
                    s=20
                )
                
        ax2.set_xlabel('Time (s)')
        ax2.set_ylabel('Request Duration (ms)')
        ax2.set_title('Request Duration Over Time')
        ax2.legend()
        ax2.grid(True, alpha=0.3)
        
        # Plot 3: Binary CPU Usage Over Time (bottom-left)
        # Shows both external (psutil) and self-reported measurements
        ax3 = axes[1, 0]
        
        # External measurements (psutil) - solid lines
        for source in ['binary_a', 'binary_b']:
            source_df = resource_df[resource_df['source'] == source]
            if len(source_df) > 0:
                ax3.plot(
                    source_df['relative_time'], 
                    source_df['cpu_percent'],
                    c=colors.get(source, 'gray'),
                    label=f'{source} (psutil)',
                    alpha=0.7,
                    linestyle='-'
                )
        
        # Self-reported measurements - dashed lines with markers
        if not self_reported_df.empty:
            for source in ['binary_a', 'binary_b']:
                source_df = self_reported_df[(self_reported_df['source'] == source) & (self_reported_df['error'].isna())]
                if len(source_df) > 0:
                    ax3.plot(
                        source_df['relative_time'], 
                        source_df['cpu_percent'],
                        c=colors.get(source, 'gray'),
                        label=f'{source} (self-reported)',
                        alpha=0.7,
                        linestyle='--',
                        marker='o',
                        markersize=3
                    )
                
        ax3.set_xlabel('Time (s)')
        ax3.set_ylabel('Binary CPU Usage (%)')
        ax3.set_title('Binary Process CPU Usage (External vs Self-Reported)')
        ax3.legend()
        ax3.grid(True, alpha=0.3)
        
        # Plot 4: Binary Memory Usage Over Time (bottom-right)
        ax4 = axes[1, 1]
        for source in ['binary_a', 'binary_b']:
            source_df = resource_df[resource_df['source'] == source]
            if len(source_df) > 0:
                ax4.plot(
                    source_df['relative_time'], 
                    source_df['memory_mb'],
                    c=colors.get(source, 'gray'),
                    label=source,
                    alpha=0.7
                )
                
        ax4.set_xlabel('Time (s)')
        ax4.set_ylabel('Memory Usage (MB)')
        ax4.set_title('Binary Process Memory Usage')
        ax4.legend()
        ax4.grid(True, alpha=0.3)
        
        plt.tight_layout()
        plt.savefig(path, dpi=150)
        plt.close()
    
    def _generate_external_plot(self, external_df: pd.DataFrame, path: Path):
        """Generate a separate plot for external process monitoring (e.g., mavlink-camera-manager)"""
        if external_df.empty:
            return
            
        fig, axes = plt.subplots(1, 2, figsize=(14, 5))
        
        process_name = self.config.external_process
        
        colors = {
            'psutil': 'green',
            'api_a': 'blue',
            'api_b': 'red'
        }
        
        labels = {
            'psutil': 'Ground Truth (psutil)',
            'api_a': 'Binary A API',
            'api_b': 'Binary B API'
        }
        
        # Plot 1: CPU Usage Over Time
        ax1 = axes[0]
        for source in ['psutil', 'api_a', 'api_b']:
            source_df = external_df[(external_df['source'] == source) & (external_df['error'].isna())]
            if len(source_df) > 0:
                linestyle = '-' if source == 'psutil' else '--'
                marker = None if source == 'psutil' else 'o'
                markersize = 4 if marker else None
                ax1.plot(
                    source_df['relative_time'],
                    source_df['cpu_percent'],
                    c=colors.get(source, 'gray'),
                    label=labels.get(source, source),
                    alpha=0.7,
                    linestyle=linestyle,
                    marker=marker,
                    markersize=markersize
                )
        
        ax1.set_xlabel('Time (s)')
        ax1.set_ylabel('CPU Usage (%)')
        ax1.set_title(f'{process_name} - CPU Usage (API vs Ground Truth)')
        ax1.legend()
        ax1.grid(True, alpha=0.3)
        
        # Plot 2: Memory Usage Over Time
        ax2 = axes[1]
        for source in ['psutil', 'api_a', 'api_b']:
            source_df = external_df[(external_df['source'] == source) & (external_df['error'].isna())]
            if len(source_df) > 0:
                linestyle = '-' if source == 'psutil' else '--'
                marker = None if source == 'psutil' else 'o'
                markersize = 4 if marker else None
                ax2.plot(
                    source_df['relative_time'],
                    source_df['memory_kb'] / 1024,  # Convert to MB
                    c=colors.get(source, 'gray'),
                    label=labels.get(source, source),
                    alpha=0.7,
                    linestyle=linestyle,
                    marker=marker,
                    markersize=markersize
                )
        
        ax2.set_xlabel('Time (s)')
        ax2.set_ylabel('Memory Usage (MB)')
        ax2.set_title(f'{process_name} - Memory Usage (API vs Ground Truth)')
        ax2.legend()
        ax2.grid(True, alpha=0.3)
        
        plt.suptitle(f'External Process Monitoring: {process_name}', fontsize=14, fontweight='bold')
        plt.tight_layout()
        plt.savefig(path, dpi=150)
        plt.close()
        
    def _save_text_report(self, df: pd.DataFrame, resource_df: pd.DataFrame, self_reported_df: pd.DataFrame, external_df: pd.DataFrame, path: Path):
        """Save detailed text report"""
        with open(path, 'w') as f:
            f.write("CPU MEASUREMENT ACCURACY TEST REPORT\n")
            f.write("=" * 60 + "\n\n")
            
            f.write(f"Test Date: {datetime.now().isoformat()}\n")
            f.write(f"Duration: {self.config.duration}s\n")
            f.write(f"Binary A: {self.config.binary_a}\n")
            f.write(f"Binary B: {self.config.binary_b}\n")
            f.write(f"Stress CPUs: {self.config.stress_cpus}\n")
            f.write(f"Concurrent Requests: {self.config.concurrent_requests}\n")
            f.write(f"Sample Interval: {self.config.sample_interval}s\n")
            f.write(f"Resource Monitor Interval: {self.config.resource_interval}s\n\n")
            
            expected_cpu = self.config.stress_cpus * 100
            f.write(f"Expected CPU: ~{expected_cpu}%\n\n")
            
            # CPU Measurements
            f.write("=" * 60 + "\n")
            f.write("CPU MEASUREMENTS\n")
            f.write("=" * 60 + "\n")
            
            for source in ['ground_truth', 'binary_a', 'binary_b']:
                source_df = df[df['source'] == source]
                valid_df = source_df[source_df['error'].isna()]
                
                f.write(f"\n{source.upper()}\n")
                f.write("-" * 40 + "\n")
                
                if len(valid_df) > 0:
                    f.write(f"Samples: {len(valid_df)}\n")
                    f.write(f"Mean: {valid_df['cpu_total'].mean():.2f}%\n")
                    f.write(f"Std: {valid_df['cpu_total'].std():.2f}%\n")
                    f.write(f"Min: {valid_df['cpu_total'].min():.2f}%\n")
                    f.write(f"Max: {valid_df['cpu_total'].max():.2f}%\n")
                    
                    # Count anomalies
                    zero_readings = len(valid_df[valid_df['cpu_total'] < 10])
                    high_readings = len(valid_df[valid_df['cpu_total'] > expected_cpu * 1.5])
                    f.write(f"Zero/Low readings (<10%): {zero_readings}\n")
                    f.write(f"High readings (>150% expected): {high_readings}\n")
                    
            # Request Duration
            f.write("\n" + "=" * 60 + "\n")
            f.write("REQUEST DURATION\n")
            f.write("=" * 60 + "\n")
            
            for source in ['binary_a', 'binary_b']:
                source_df = df[(df['source'] == source) & (df['error'].isna())]
                
                f.write(f"\n{source.upper()}\n")
                f.write("-" * 40 + "\n")
                
                if len(source_df) > 0:
                    f.write(f"Mean: {source_df['duration_ms'].mean():.2f} ms\n")
                    f.write(f"Std: {source_df['duration_ms'].std():.2f} ms\n")
                    f.write(f"Min: {source_df['duration_ms'].min():.2f} ms\n")
                    f.write(f"Max: {source_df['duration_ms'].max():.2f} ms\n")
                    f.write(f"P50: {source_df['duration_ms'].quantile(0.50):.2f} ms\n")
                    f.write(f"P95: {source_df['duration_ms'].quantile(0.95):.2f} ms\n")
                    f.write(f"P99: {source_df['duration_ms'].quantile(0.99):.2f} ms\n")
                    
            # Resource Usage (external via psutil)
            f.write("\n" + "=" * 60 + "\n")
            f.write("BINARY RESOURCE USAGE (External - psutil)\n")
            f.write("=" * 60 + "\n")
            
            for source in ['binary_a', 'binary_b']:
                source_df = resource_df[resource_df['source'] == source]
                
                f.write(f"\n{source.upper()}\n")
                f.write("-" * 40 + "\n")
                
                if len(source_df) > 0:
                    f.write(f"Samples: {len(source_df)}\n")
                    f.write(f"CPU Mean: {source_df['cpu_percent'].mean():.2f}%\n")
                    f.write(f"CPU Max: {source_df['cpu_percent'].max():.2f}%\n")
                    f.write(f"Memory Mean: {source_df['memory_mb'].mean():.2f} MB\n")
                    f.write(f"Memory Max: {source_df['memory_mb'].max():.2f} MB\n")
            
            # Self-reported Resource Usage
            if not self_reported_df.empty:
                f.write("\n" + "=" * 60 + "\n")
                f.write("SELF-REPORTED RESOURCE USAGE (from /system/process)\n")
                f.write("=" * 60 + "\n")
                
                for source in ['binary_a', 'binary_b']:
                    source_df = self_reported_df[(self_reported_df['source'] == source) & (self_reported_df['error'].isna())]
                    
                    f.write(f"\n{source.upper()}\n")
                    f.write("-" * 40 + "\n")
                    
                    if len(source_df) > 0:
                        f.write(f"Samples: {len(source_df)}\n")
                        f.write(f"CPU Mean: {source_df['cpu_percent'].mean():.2f}%\n")
                        f.write(f"CPU Max: {source_df['cpu_percent'].max():.2f}%\n")
                        f.write(f"CPU Min: {source_df['cpu_percent'].min():.2f}%\n")
                        f.write(f"Memory Mean: {source_df['memory_kb'].mean() / 1024:.2f} MB\n")
                        f.write(f"Memory Max: {source_df['memory_kb'].max() / 1024:.2f} MB\n")
            
            # External Process Monitoring
            if not external_df.empty:
                f.write("\n" + "=" * 60 + "\n")
                f.write(f"EXTERNAL PROCESS: {self.config.external_process}\n")
                f.write("=" * 60 + "\n")
                
                for source in ['psutil', 'api_a', 'api_b']:
                    source_df = external_df[(external_df['source'] == source) & (external_df['error'].isna())]
                    
                    source_label = {
                        'psutil': 'GROUND TRUTH (psutil)',
                        'api_a': 'BINARY A API',
                        'api_b': 'BINARY B API'
                    }.get(source, source.upper())
                    
                    f.write(f"\n{source_label}\n")
                    f.write("-" * 40 + "\n")
                    
                    if len(source_df) > 0:
                        f.write(f"Samples: {len(source_df)}\n")
                        f.write(f"CPU Mean: {source_df['cpu_percent'].mean():.2f}%\n")
                        f.write(f"CPU Max: {source_df['cpu_percent'].max():.2f}%\n")
                        f.write(f"CPU Min: {source_df['cpu_percent'].min():.2f}%\n")
                        f.write(f"Memory Mean: {source_df['memory_kb'].mean() / 1024:.2f} MB\n")
                        f.write(f"Memory Max: {source_df['memory_kb'].max() / 1024:.2f} MB\n")
                    
    def _cleanup(self):
        """Clean up all started processes"""
        for proc in self._processes:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
                    
        # Also kill any stray stress processes
        try:
            subprocess.run(["killall", "stress"], 
                          capture_output=True, timeout=5)
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(
        description="CPU Measurement Accuracy Test for linux2rest"
    )
    parser.add_argument(
        "--binary-a", "-a",
        type=Path,
        required=True,
        help="Path to first binary (e.g., master build)"
    )
    parser.add_argument(
        "--binary-b", "-b", 
        type=Path,
        required=True,
        help="Path to second binary (e.g., fix_sampling build)"
    )
    parser.add_argument(
        "--port-a",
        type=int,
        default=26030,
        help="Port for binary A (default: 26030)"
    )
    parser.add_argument(
        "--port-b",
        type=int,
        default=26031,
        help="Port for binary B (default: 26031)"
    )
    parser.add_argument(
        "--duration", "-d",
        type=int,
        default=60,
        help="Test duration in seconds (default: 60)"
    )
    parser.add_argument(
        "--concurrent", "-c",
        type=int,
        default=2,
        help="Number of concurrent requests per sample (default: 2)"
    )
    parser.add_argument(
        "--interval", "-i",
        type=float,
        default=1.0,
        help="Sample interval in seconds (default: 1.0)"
    )
    parser.add_argument(
        "--resource-interval", "-r",
        type=float,
        default=0.1,
        help="Resource monitor interval in seconds (default: 0.1)"
    )
    parser.add_argument(
        "--stress-cpus", "-s",
        type=int,
        default=2,
        help="Number of stress CPU workers (default: 2)"
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=Path("./results"),
        help="Output directory for results (default: ./results)"
    )
    parser.add_argument(
        "--warmup", "-w",
        type=int,
        default=0,
        help="Warmup time in seconds (default: 0, capture entire runtime)"
    )
    # Extra arguments for binaries - no default log-settings to avoid compatibility issues
    parser.add_argument(
        "--args-a",
        type=str,
        default="",
        help="Extra arguments for binary A (e.g., --log-settings system-cpu=10)"
    )
    parser.add_argument(
        "--args-b",
        type=str,
        default="",
        help="Extra arguments for binary B (e.g., --log-settings system-cpu=10)"
    )
    parser.add_argument(
        "--external-process", "-e",
        type=str,
        default=None,
        help="Optional: Monitor an external process (e.g., 'mavlink-camera-manager') and compare API vs psutil"
    )
    
    args = parser.parse_args()
    
    # Validate binaries exist
    if not args.binary_a.exists():
        print(f"Error: Binary A not found: {args.binary_a}")
        sys.exit(1)
    if not args.binary_b.exists():
        print(f"Error: Binary B not found: {args.binary_b}")
        sys.exit(1)
        
    config = Config(
        binary_a=args.binary_a,
        binary_b=args.binary_b,
        port_a=args.port_a,
        port_b=args.port_b,
        args_a=args.args_a.split() if args.args_a else [],
        args_b=args.args_b.split() if args.args_b else [],
        duration=args.duration,
        sample_interval=args.interval,
        top_interval=args.interval,
        resource_interval=args.resource_interval,
        concurrent_requests=args.concurrent,
        stress_cpus=args.stress_cpus,
        output_dir=args.output,
        warmup_time=args.warmup,
        external_process=args.external_process
    )
    
    test = CPUAccuracyTest(config)
    
    # Handle Ctrl+C gracefully
    def signal_handler(sig, frame):
        print("\nInterrupted! Cleaning up...")
        test._cleanup()
        sys.exit(1)
        
    signal.signal(signal.SIGINT, signal_handler)
    
    # Run test
    asyncio.run(test.run())


if __name__ == "__main__":
    main()
