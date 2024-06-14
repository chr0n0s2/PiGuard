#!/bin/bash

# Define installation paths
INSTALL_DIR="/usr/local/bin"
SERVICE_DIR="/etc/systemd/system"
CONFIG_FILE="path_config.txt"

# Get the current directory
CURRENT_DIR=$(pwd)

# Download piguard.service to the appropriate directory
sudo wget --header="Authorization: token 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" -O "$SERVICE_DIR/piguard.service" https://raw.githubusercontent.com/chr0n0s2/PiGuard/main/piguard.service
sudo chmod 644 "$SERVICE_DIR/piguard.service"

# Download piguard.py to the appropriate directory
sudo wget --header="Authorization: token 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" -O "$INSTALL_DIR/piguard.pyc" https://raw.githubusercontent.com/chr0n0s2/PiGuard/main/piguard.pyc
sudo chmod 755 "$INSTALL_DIR/piguard.pyc"

# Create the configuration file with the current directory path
echo "DEFAULT_CONFIG_FILE = $CURRENT_DIR/piguard_config.txt" | sudo tee "$INSTALL_DIR/$CONFIG_FILE" > /dev/null

# Set read and write permissions for all users on the configuration file
sudo chmod 666 "$INSTALL_DIR/$CONFIG_FILE"

# Reload systemd manager configuration to recognize new service
sudo systemctl daemon-reload

# Enable and start the piguard service
sudo systemctl enable piguard.service
sudo systemctl start piguard.service

echo "Installation completed."
