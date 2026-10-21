#!/bin/bash
# at1 - AWS Lightsail launch script (Ubuntu 24.04 LTS).
#
# Paste this whole file into Lightsail -> Create instance -> "Add launch script".
# It runs once, as root, on first boot (about 3-5 minutes). Progress and errors
# land in /var/log/cloud-init-output.log on the instance.
#
# Result: uvicorn runs as a systemd service on 127.0.0.1:8777, restarts on crash
# and on reboot, and Caddy publishes it on port 80 (or HTTPS when DOMAIN is set).
#
# Later code updates: push to GitHub, then on the instance run  sudo at1-update

# ============ EDIT THESE BEFORE PASTING =====================================
# Private repo: put a fine-grained GitHub token with read-only "Contents"
# access on this one repo in the URL:  https://<TOKEN>@github.com/<you>/at1.git
REPO_URL="https://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@github.com/sandeepams5856/at1.git"
BRANCH="main"

# Optional. Leave both empty for no login. With a password the browser asks
# once and remembers it - refreshing the dashboard does not ask again.
AT1_USER=""
AT1_PASSWORD=""

# Optional. A domain whose DNS A record points at this instance's static IP
# turns on automatic HTTPS (also open port 443 in the Lightsail firewall tab).
# Empty = plain http://<static-ip>.
DOMAIN=""
# ============================================================================

set -euo pipefail
exec > >(tee -a /var/log/at1-setup.log) 2>&1
echo "=== at1 setup started $(date)"

APP_DIR=/opt/at1
SRC="$APP_DIR/src"
PY_VERSION=3.14

# --- System ----------------------------------------------------------------
# targets.py reads the market clock with a naive datetime.now(), so the box must
# run on IST - on the UTC default every session boundary is 5.5 hours off.
timedatectl set-timezone Asia/Kolkata

# pandas + numpy wheels plus the running app crowd 1 GB; swap keeps pip and a
# burst of chart requests from being OOM-killed.
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git curl ca-certificates gnupg debian-keyring debian-archive-keyring apt-transport-https

# --- Caddy (reverse proxy; HTTPS + optional password) ------------------------
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  > /etc/apt/sources.list.d/caddy-stable.list
apt-get update -y
apt-get install -y caddy

# --- uv + Python -------------------------------------------------------------
# Ubuntu ships 3.12; uv fetches the same 3.14 the Windows venv uses. Pythons go
# under /opt/at1 rather than /root so the unprivileged service user can run them.
curl -LsSf https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL=/usr/local/bin sh
export UV_PYTHON_INSTALL_DIR="$APP_DIR/python"

id at1 >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin at1
mkdir -p "$APP_DIR"

# --- App code ----------------------------------------------------------------
if [ -d "$SRC/.git" ]; then
  git -C "$SRC" fetch --depth 1 origin "$BRANCH"
  git -C "$SRC" reset --hard "origin/$BRANCH"
else
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$SRC"
fi

uv python install "$PY_VERSION"
uv venv --python "$PY_VERSION" "$APP_DIR/venv"
uv pip install --python "$APP_DIR/venv/bin/python" -r "$SRC/requirements.txt"
chown -R at1:at1 "$APP_DIR"

# Fail here, with the traceback in the setup log, rather than leaving systemd
# in a silent restart loop.
sudo -u at1 bash -c "cd '$SRC' && '$APP_DIR/venv/bin/python' -c 'import app.main'"

# --- systemd service -----------------------------------------------------------
# One worker on purpose: the price and option-chain caches live in process
# memory, and a second worker would double the Yahoo/NSE traffic and the RAM.
cat > /etc/systemd/system/at1.service <<EOF
[Unit]
Description=at1 NSE/BSE signal dashboard
After=network-online.target
Wants=network-online.target

[Service]
User=at1
Group=at1
WorkingDirectory=$SRC
Environment=TZ=Asia/Kolkata
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8777 --proxy-headers
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now at1

# --- Caddyfile -----------------------------------------------------------------
SITE="${DOMAIN:-:80}"
{
  echo "$SITE {"
  echo "    encode gzip"
  if [ -n "$AT1_PASSWORD" ]; then
    HASH=$(caddy hash-password --plaintext "$AT1_PASSWORD")
    echo "    basic_auth {"
    echo "        ${AT1_USER:-at1} $HASH"
    echo "    }"
  fi
  echo "    reverse_proxy 127.0.0.1:8777"
  echo "}"
} > /etc/caddy/Caddyfile
systemctl restart caddy

# --- Update helper: sudo at1-update --------------------------------------------
cat > /usr/local/bin/at1-update <<EOF
#!/bin/bash
# Pull the latest code from GitHub, refresh packages, restart the dashboard.
set -euo pipefail
git -C "$SRC" fetch --depth 1 origin "$BRANCH"
git -C "$SRC" reset --hard "origin/$BRANCH"
uv pip install --python "$APP_DIR/venv/bin/python" -r "$SRC/requirements.txt"
chown -R at1:at1 "$APP_DIR"
sudo -u at1 bash -c "cd '$SRC' && '$APP_DIR/venv/bin/python' -c 'import app.main'"
systemctl restart at1
echo "at1 updated and restarted."
EOF
chmod 755 /usr/local/bin/at1-update

echo "=== at1 setup finished $(date)"
