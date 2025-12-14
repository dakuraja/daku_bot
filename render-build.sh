#!/usr/bin/env bash
set -o errexit

echo "Installing wkhtmltopdf..."

apt-get update
apt-get install -y wget xfonts-75dpi xfonts-base

wget https://github.com/wkhtmltopdf/wkhtmltopdf/releases/download/0.12.6/wkhtmltox_0.12.6-1.focal_amd64.deb

apt install -y ./wkhtmltox_0.12.6-1.focal_amd64.deb

wkhtmltopdf --version
