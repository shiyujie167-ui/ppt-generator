#!/usr/bin/env bash

set -Eeuo pipefail
umask 0027

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "请用 sudo 运行此脚本" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT=/opt/ppt-web
GIT_DIR=/srv/git/ppt-web.git
APP_USER=ppt-web
APP_GROUP=ppt-web
DEPLOY_USER="${DEPLOY_USER:-${SUDO_USER:-azureadmin}}"
NGINX_SITE=/etc/nginx/sites-available/okr
NGINX_INCLUDE='    include /etc/nginx/snippets/ppt-web.conf;'

getent passwd "$DEPLOY_USER" >/dev/null || {
  echo "部署用户不存在：$DEPLOY_USER" >&2
  exit 3
}

if [[ "${SKIP_APT:-0}" != 1 ]]; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3-venv fontconfig fonts-noto-cjk
fi

getent group "$APP_GROUP" >/dev/null || groupadd --system "$APP_GROUP"
if ! getent passwd "$APP_USER" >/dev/null; then
  useradd --system --gid "$APP_GROUP" --home-dir /nonexistent \
    --shell /usr/sbin/nologin "$APP_USER"
fi

install -d -o root -g root -m 0755 /srv/git
if [[ ! -d "$GIT_DIR" ]]; then
  runuser -u "$DEPLOY_USER" -- git init --bare "$GIT_DIR"
fi
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$GIT_DIR"
chmod 0700 "$GIT_DIR"
git --git-dir="$GIT_DIR" config receive.denyNonFastForwards true
git --git-dir="$GIT_DIR" config receive.denyDeletes true

install -d -o root -g "$APP_GROUP" -m 0750 \
  "$APP_ROOT" "$APP_ROOT/releases" "$APP_ROOT/scripts" "$APP_ROOT/backups"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0750 \
  "$APP_ROOT/data" "$APP_ROOT/projects"
install -d -o root -g root -m 0755 /etc/ppt-web
if [[ ! -f /etc/ppt-web/ppt-web.env ]]; then
  install -o root -g "$APP_GROUP" -m 0640 \
    "$SCRIPT_DIR/ppt-web.env.example" /etc/ppt-web/ppt-web.env
fi

install -o root -g "$APP_GROUP" -m 0750 \
  "$SCRIPT_DIR/server-deploy.sh" "$APP_ROOT/scripts/deploy.sh"
install -o root -g "$APP_GROUP" -m 0750 \
  "$SCRIPT_DIR/backup.sh" "$APP_ROOT/scripts/backup.sh"
install -o root -g root -m 0644 \
  "$SCRIPT_DIR/ppt-web.service" /etc/systemd/system/ppt-web.service
install -o root -g root -m 0644 \
  "$SCRIPT_DIR/ppt-web-backup.service" /etc/systemd/system/ppt-web-backup.service
install -o root -g root -m 0644 \
  "$SCRIPT_DIR/ppt-web-backup.timer" /etc/systemd/system/ppt-web-backup.timer
snippet_backup=""
if [[ -f /etc/nginx/snippets/ppt-web.conf ]]; then
  snippet_backup="$(mktemp /tmp/nginx-ppt-web.before.XXXXXX)"
  cp /etc/nginx/snippets/ppt-web.conf "$snippet_backup"
fi
install -o root -g root -m 0644 \
  "$SCRIPT_DIR/nginx-ppt-web.conf" /etc/nginx/snippets/ppt-web.conf

site_backup=""
if ! grep -Fq "$NGINX_INCLUDE" "$NGINX_SITE"; then
  site_backup="$NGINX_SITE.before-ppt-web-$(date -u '+%Y%m%d-%H%M%S')"
  cp "$NGINX_SITE" "$site_backup"
  closing_line="$(grep -n '^}' "$NGINX_SITE" | tail -1 | cut -d: -f1)"
  [[ -n "$closing_line" ]] || {
    echo "无法定位 Nginx server 结尾：$NGINX_SITE" >&2
    exit 4
  }
  sed -i "${closing_line}i\\
$NGINX_INCLUDE
" "$NGINX_SITE"
fi

if ! nginx -t; then
  [[ -n "$site_backup" ]] && cp "$site_backup" "$NGINX_SITE"
  if [[ -n "$snippet_backup" ]]; then
    cp "$snippet_backup" /etc/nginx/snippets/ppt-web.conf
  else
    rm -f /etc/nginx/snippets/ppt-web.conf
  fi
  nginx -t
  echo "Nginx 配置校验失败，已恢复原配置" >&2
  exit 5
fi
[[ -n "$snippet_backup" ]] && rm -f "$snippet_backup"
systemctl daemon-reload
systemctl enable ppt-web.service ppt-web-backup.timer >/dev/null
systemctl reload nginx

echo "bootstrap_status=success"
echo "git_repo=$GIT_DIR"
echo "env_file=/etc/ppt-web/ppt-web.env"
echo "next=sudo $APP_ROOT/scripts/deploy.sh <main-commit>"
