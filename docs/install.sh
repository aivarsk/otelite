#!/bin/bash
set -e
VERSION=$(curl -s "https://api.github.com/repos/aivarsk/otelite/releases/latest" | grep '"tag_name":' | sed -E 's/.*"([^"]+)".*/\1/')

if [ "$EUID" -eq 0 ]; then
  echo "WARNING: Installing as root. For user-level install, run without sudo."
  read -p "Continue with system-wide install? [y/N] " confirm </dev/tty
  [[ "$confirm" == [yY]* ]] || exit 1

  LIB_DIR="/usr/local/lib/otelite"
  BIN_DIR="/usr/local/bin"
  SYSTEMD_DIR="/etc/systemd/system"
  USER=""
  SYSTEMD_CFG="User=root
Group=root
[Install]
WantedBy=multi-user.target
"
else
  LIB_DIR="${HOME}/.local/lib/otelite"
  BIN_DIR="${HOME}/.local/bin"
  SYSTEMD_DIR="${HOME}/.config/systemd/user"
  USER="--user"
  SYSTEMD_CFG="[Install]
WantedBy=default.target
"
fi

mkdir -p "${BIN_DIR}" "${LIB_DIR}"

curl -fsSL "https://github.com/aivarsk/otelite/releases/download/${VERSION}/otelite-${VERSION}.pyz" -o "${LIB_DIR}/otelite.pyz"
chmod +x "${LIB_DIR}/otelite.pyz"

cat >"${BIN_DIR}/otelite" <<EOF
#!/bin/bash
ulimit -s 1024
exec python3 "${LIB_DIR}/otelite.pyz" "\$@"
EOF
chmod +x "${BIN_DIR}/otelite"

cat >"${SYSTEMD_DIR}/otelite.service" <<EOF
[Unit]
Description=Small OTEL backend for test and dev.
After=network.target

[Service]
Type=simple
ExecStart=${BIN_DIR}/otelite --daemon
Restart=always
RestartSec=10
LimitSTACK=1M
${SYSTEMD_CFG}
EOF

systemctl ${USER} daemon-reload
systemctl ${USER} enable otelite.service
systemctl ${USER} start otelite
systemctl ${USER} status otelite
