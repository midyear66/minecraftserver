#!/bin/bash
# First-time setup script for Minecraft Server Manager

set -e

echo "Minecraft Server Manager - Setup"
echo "================================="

# Create .env from example if it doesn't exist
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "Created .env from .env.example"
    else
        touch .env
        echo "Created empty .env file"
    fi
fi

# Auto-detect and set DOCKER_GID if not already set
if ! grep -q "^DOCKER_GID=" .env || [ -z "$(grep "^DOCKER_GID=" .env | cut -d= -f2)" ]; then
    DOCKER_GID=$(getent group docker | cut -d: -f3)
    if [ -n "$DOCKER_GID" ]; then
        # Remove any existing empty DOCKER_GID line and add the correct one
        grep -v "^DOCKER_GID=" .env > .env.tmp || true
        mv .env.tmp .env
        echo "DOCKER_GID=$DOCKER_GID" >> .env
        echo "Set DOCKER_GID=$DOCKER_GID (auto-detected)"
    else
        echo "Warning: Could not detect docker group ID. You may need to set DOCKER_GID manually."
    fi
else
    echo "DOCKER_GID already set in .env"
fi

# Create required directories
mkdir -p mc_data backups logs proxy
echo "Created required directories"

# Remind about credentials
echo ""
echo "Setup complete! Before starting:"
echo "  1. Edit .env to set ADMIN_USERNAME and ADMIN_PASSWORD"
echo "  2. Run: docker compose up -d"
echo ""
