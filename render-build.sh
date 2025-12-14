#!/usr/bin/env bash
set -e

echo "Updating system..."
apt-get update -y

echo "Installing base dependencies..."
apt-get install -y wget fontconfig xfonts-75dpi xfonts-base

echo "Downloading wkhtmltopdf..."
wget -q https://github.com/wkhtmltopdf/wkhtmltopdf/releases/download/0.12.6/wkhtmltox_0.12.6-1.focal_amd64.deb

echo "Installing wkhtmltopdf..."
dpkg -i wkhtmltox_0.12.6-1.focal_amd64.deb || apt-get -f install -y

echo "Verifying installation..."
wkhtmltopdf --version
