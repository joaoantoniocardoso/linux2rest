# Multi-architecture Docker image for linux2rest CPU accuracy tests
# Supports: linux/amd64, linux/arm64, linux/arm/v7
#
# Build multi-arch:
#   docker buildx build --platform linux/amd64,linux/arm64,linux/arm/v7 -t linux2rest-test .
#
# Run:
#   docker run -v $(pwd)/binaries:/binaries -v $(pwd)/results:/app/results linux2rest-test \
#       -a /binaries/linux2rest_master -b /binaries/linux2rest_fix_sampling

FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    stress \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
# Use piwheels as PRIMARY index for ARM to get pre-built wheels
COPY requirements.txt .
ARG TARGETARCH
RUN if [ "$TARGETARCH" = "arm" ]; then \
        pip install --no-cache-dir \
            --index-url https://www.piwheels.org/simple \
            --extra-index-url https://pypi.org/simple \
            -r requirements.txt; \
    else \
        pip install --no-cache-dir -r requirements.txt; \
    fi

# Copy the test script
COPY cpu_accuracy_test.py .

# Create results directory
RUN mkdir -p /app/results

# Default entrypoint runs the test
ENTRYPOINT ["python3", "cpu_accuracy_test.py"]

# Default arguments (can be overridden)
CMD ["--help"]
