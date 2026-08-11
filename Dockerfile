FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    tmux \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy application code
COPY . .
RUN pip install --no-cache-dir .

# Create directories for data
RUN mkdir -p /app/data /app/logs /app/docs

# Expose MCP server port
EXPOSE 8000

# Default command (can be overridden)
CMD ["python", "run_server.py"]
