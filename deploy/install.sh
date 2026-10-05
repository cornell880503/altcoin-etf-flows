#!/usr/bin/env bash
# Install the "山寨幣 ETF 資金流" dashboard on a Debian or Ubuntu server (for example a Linode
# Nanode). Run it on the server, as root:
#
#   curl -fsSL https://raw.githubusercontent.com/cornell880503/altcoin-etf-flows/main/deploy/install.sh | sudo bash
#
# Optional settings, placed before "bash" (e.g. "... | sudo DOMAIN=etf.example.com bash"):
#   DOMAIN=etf.example.com   also serve https://etf.example.com (its DNS A record must point here)
#   SITE_PASSWORD=...        ask visitors for a password (user name: etf)
#   AUTO_UPDATES=0           leave the system's automatic security updates alone
#
# What it sets up:
#   - Caddy, a small web server that serves /var/www/altetf (and gets HTTPS certificates itself)
#   - /usr/local/bin/altetf-update and a systemd timer that runs it at :07 and :37 every hour: it
#     downloads the page GitHub Actions rebuilt (branch "site" of the repository), checks it and
#     swaps it in, so a failed build never replaces a good page
# The server needs no keys or tokens (the repository is public). Running the script again is safe.
set -euo pipefail

REPO="cornell880503/altcoin-etf-flows"
BRANCH="site"
WEB="/var/www/altetf"
SVC_USER="altetf"
DOMAIN="${DOMAIN:-}"
SITE_PASSWORD="${SITE_PASSWORD:-}"
AUTO_UPDATES="${AUTO_UPDATES:-1}"
RENDER_ONLY="${RENDER_ONLY:-}"   # for testing: write the generated files to this folder and stop

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------ generated files
render_updater() {
	cat <<EOF
#!/usr/bin/env bash
# Fetch the dashboard page that GitHub Actions rebuilt and swap it in if it looks right.
# Installed by deploy/install.sh from https://github.com/${REPO}
set -euo pipefail
BASE="\${ALTETF_BASE:-https://raw.githubusercontent.com/${REPO}/${BRANCH}}"
WEB="\${ALTETF_WEB:-${WEB}}"
tmp="\$(mktemp -d "\$WEB/.incoming.XXXXXX")"
trap 'rm -rf "\$tmp"' EXIT
get() { curl -fsS --retry 3 --retry-delay 20 --max-time 120 -H 'Cache-Control: no-cache' "\$BASE/\$1" -o "\$tmp/\$1"; }
get index.html
get health.json || true
size=\$(stat -c %s "\$tmp/index.html")
if [ "\$size" -lt 100000 ] || ! grep -q 'name="altetf-built"' "\$tmp/index.html" || ! tail -c 64 "\$tmp/index.html" | grep -q '</html>'; then
	echo "the downloaded page looks wrong (\$size bytes); keeping the current one" >&2
	exit 1
fi
built=\$(grep -o 'name="altetf-built" content="[^"]*"' "\$tmp/index.html" | sed 's/.*content="//; s/"\$//')
if [ -f "\$WEB/index.html" ] && cmp -s "\$tmp/index.html" "\$WEB/index.html"; then
	echo "unchanged (built \$built)"
	exit 0
fi
chmod 644 "\$tmp"/*
if [ -s "\$tmp/health.json" ]; then mv -f "\$tmp/health.json" "\$WEB/health.json"; fi
mv -f "\$tmp/index.html" "\$WEB/index.html"
echo "updated (built \$built)"
EOF
}

render_service() {
	cat <<EOF
[Unit]
Description=Fetch the latest altcoin ETF dashboard page
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=${SVC_USER}
Group=${SVC_USER}
ExecStart=/usr/local/bin/altetf-update
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ReadWritePaths=${WEB}
EOF
}

render_timer() {
	cat <<'EOF'
[Unit]
Description=Fetch the altcoin ETF dashboard page twice an hour

[Timer]
OnCalendar=*:07,37
RandomizedDelaySec=60
Persistent=true

[Install]
WantedBy=timers.target
EOF
}

render_caddyfile() {
	local auth="" hash
	if [ -n "$SITE_PASSWORD" ]; then
		hash="$(caddy hash-password --plaintext "$SITE_PASSWORD")"
		auth=$'\tbasicauth {\n\t\tetf '"$hash"$'\n\t}\n'
	fi
	cat <<EOF
# Written by altcoin-etf-flows deploy/install.sh. Running the script again rewrites this file.
(altetf) {
	root * ${WEB}
${auth}	encode zstd gzip
	header {
		X-Content-Type-Options nosniff
		Referrer-Policy strict-origin-when-cross-origin
		X-Frame-Options SAMEORIGIN
		-Server
	}
	header /index.html Cache-Control "no-cache"
	header / Cache-Control "no-cache"
	file_server {
		hide .*
	}
}

:80 {
	import altetf
}
EOF
	if [ -n "$DOMAIN" ]; then
		cat <<EOF

${DOMAIN} {
	import altetf
}
EOF
	fi
}

if [ -n "$RENDER_ONLY" ]; then
	mkdir -p "$RENDER_ONLY"
	render_updater >"$RENDER_ONLY/altetf-update"
	render_service >"$RENDER_ONLY/altetf-update.service"
	render_timer >"$RENDER_ONLY/altetf-update.timer"
	render_caddyfile >"$RENDER_ONLY/Caddyfile"
	chmod 755 "$RENDER_ONLY/altetf-update"
	echo "rendered into $RENDER_ONLY"
	exit 0
fi

# ------------------------------------------------------------------ checks
[ "$(id -u)" -eq 0 ] || die "please run as root, e.g. with sudo"
command -v apt-get >/dev/null || die "this script supports Debian and Ubuntu (apt) only"
command -v systemctl >/dev/null || die "systemd is required"
if [ -n "$DOMAIN" ] && ! [[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then die "DOMAIN looks wrong: $DOMAIN"; fi
export DEBIAN_FRONTEND=noninteractive

# ------------------------------------------------------------------ packages
say "Installing packages"
apt-get update -qq
apt-get install -y -qq curl ca-certificates gnupg >/dev/null
if ! command -v caddy >/dev/null; then
	# the official Caddy repository keeps Caddy current; fall back to the distribution's package
	if curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg &&
		curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt >/etc/apt/sources.list.d/caddy-stable.list &&
		chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list &&
		apt-get update -qq && apt-get install -y -qq caddy >/dev/null; then
		echo "Caddy installed from the official repository"
	else
		rm -f /etc/apt/sources.list.d/caddy-stable.list
		apt-get update -qq
		apt-get install -y -qq caddy >/dev/null || die "could not install Caddy"
		echo "Caddy installed from the distribution"
	fi
fi
if [ "$AUTO_UPDATES" = "1" ]; then
	apt-get install -y -qq unattended-upgrades >/dev/null || true
	if [ ! -f /etc/apt/apt.conf.d/20auto-upgrades ]; then
		printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' >/etc/apt/apt.conf.d/20auto-upgrades
	fi
fi

# ------------------------------------------------------------------ updater + timer
say "Setting up the page updater"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$SVC_USER"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 755 "$WEB"
render_updater >/usr/local/bin/altetf-update
chmod 755 /usr/local/bin/altetf-update
render_service >/etc/systemd/system/altetf-update.service
render_timer >/etc/systemd/system/altetf-update.timer
systemctl daemon-reload
systemctl enable --now altetf-update.timer >/dev/null
if systemctl start altetf-update.service; then
	journalctl -u altetf-update.service -n 1 --no-pager -o cat || true
else
	echo "The first download failed (see: journalctl -u altetf-update -n 20). The timer will retry."
fi
if [ ! -f "$WEB/index.html" ]; then
	printf '<!doctype html><meta charset="utf-8"><title>山寨幣 ETF 資金流</title><p style="font-family:sans-serif">網頁還在準備中，30 分鐘內會自動出現。</p>\n' >"$WEB/index.html"
	chown "$SVC_USER:$SVC_USER" "$WEB/index.html"
fi

# ------------------------------------------------------------------ web server
say "Configuring Caddy"
if [ -f /etc/caddy/Caddyfile ] && ! grep -q 'altcoin-etf-flows deploy/install.sh' /etc/caddy/Caddyfile; then
	cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.before-altetf.$(date +%Y%m%d%H%M%S)"
fi
render_caddyfile >/etc/caddy/Caddyfile.new
caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile.new >/dev/null 2>&1 || {
	caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile.new || true
	die "the generated Caddyfile did not validate (left at /etc/caddy/Caddyfile.new)"
}
mv -f /etc/caddy/Caddyfile.new /etc/caddy/Caddyfile
systemctl enable caddy >/dev/null 2>&1 || true
systemctl restart caddy

# open the firewall if one is running on this machine
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; then
	ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null && echo "ufw: opened ports 80 and 443"
elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
	firewall-cmd --permanent --add-service=http --add-service=https >/dev/null && firewall-cmd --reload >/dev/null && echo "firewalld: opened http and https"
fi

# ------------------------------------------------------------------ done
IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "src") { print $(i + 1); exit }}')" || IP=""
say "Done"
echo "Open:  http://${IP:-<this server>}/"
if [ -n "$DOMAIN" ]; then echo "       https://${DOMAIN}/  (once its DNS A record points to ${IP:-this server})"; fi
if [ -n "$SITE_PASSWORD" ]; then echo "Login: user etf, the password you set"; fi
cat <<'EOF'

The page refreshes itself: GitHub rebuilds it every 3 hours, this server fetches it at :07 and :37.
  status          systemctl list-timers altetf-update.timer; cat /var/www/altetf/health.json
  fetch now       sudo systemctl start altetf-update
  recent runs     journalctl -u altetf-update -n 20
If the page does not open, check that inbound TCP 80 (and 443 for a domain) is allowed in the
Linode Cloud Firewall, if one is attached to this server.
EOF
