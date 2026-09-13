#!/bin/sh
# Installs mcbackup to /opt/mcbackup, the `mcbackup` CLI, and the systemd service.
set -e
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo ./install.sh)"; exit 1; }
command -v restic >/dev/null || { echo "installing restic"; apt-get install -y restic >/dev/null; }
command -v rsync  >/dev/null || apt-get install -y rsync >/dev/null
DEST=/opt/mcbackup
mkdir -p "$DEST/restores" "$DEST/tmp"
install -m 755 mcbackup.py "$DEST/mcbackup.py"
[ -f "$DEST/config.toml" ] || cp config.example.toml "$DEST/config.toml"
if [ ! -f "$DEST/env" ]; then
    sed "s|^RESTIC_PASSWORD=.*|RESTIC_PASSWORD=$(openssl rand -base64 32 | tr -d '/+=' | cut -c1-32)|" env.example > "$DEST/env"
    chmod 600 "$DEST/env"
fi
printf '#!/bin/sh\n[ "$(id -u)" -eq 0 ] || exec sudo "$0" "$@"\nexec /usr/bin/python3 %s/mcbackup.py "$@"\n' "$DEST" > /usr/local/bin/mcbackup
chmod 755 /usr/local/bin/mcbackup
install -m 644 mcbackup.service /etc/systemd/system/mcbackup.service
systemctl daemon-reload
cat <<MSG
installed.
  1. edit $DEST/env      - R2 endpoint + keys, MCSManager API key. SAVE THE RESTIC_PASSWORD SOMEWHERE SAFE.
  2. edit $DEST/config.toml (optional) - policy, retention, manual sources
  3. mcbackup init && mcbackup discover
  4. systemctl enable --now mcbackup
MSG
