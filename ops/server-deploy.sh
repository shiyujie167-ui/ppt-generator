#!/usr/bin/env bash

set -Eeuo pipefail
umask 0027

APP_ROOT=/opt/ppt-web
GIT_DIR=/srv/git/ppt-web.git
RELEASES_DIR="$APP_ROOT/releases"
CURRENT_LINK="$APP_ROOT/current"
DATA_DIR="$APP_ROOT/data"
PROJECTS_DIR="$APP_ROOT/projects"
BACKUP_DIR="$APP_ROOT/backups"
DEPLOY_LOG="$BACKUP_DIR/deployments.log"
DEPLOY_SCRIPT="$APP_ROOT/scripts/deploy.sh"
BACKUP_SCRIPT="$APP_ROOT/scripts/backup.sh"
ENV_FILE=/etc/ppt-web/ppt-web.env
SYSTEMD_UNIT=/etc/systemd/system/ppt-web.service
SERVICE_NAME=ppt-web.service
NGINX_SNIPPET=/etc/nginx/snippets/ppt-web.conf
LOCK_FILE=/run/lock/ppt-web-deploy.lock
HEALTH_URL=http://127.0.0.1:8080/api/health
APP_USER=ppt-web
APP_GROUP=ppt-web

if [[ "$#" -ne 1 || ! "$1" =~ ^[0-9a-f]{40,64}$ ]]; then
  echo "用法：$0 <完整提交 SHA>" >&2
  exit 2
fi
commit="$1"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "已有 ppt-web 部署正在执行" >&2
  exit 3
fi

if ! git --git-dir="$GIT_DIR" cat-file -e "$commit^{commit}"; then
  echo "服务器 Git 仓库中不存在提交：$commit" >&2
  exit 4
fi
main_commit="$(git --git-dir="$GIT_DIR" rev-parse refs/heads/main)"
if [[ "$commit" != "$main_commit" ]]; then
  echo "拒绝部署：请求提交不是服务器 main 当前提交" >&2
  echo "main=$main_commit requested=$commit" >&2
  exit 5
fi

install -d -o root -g "$APP_GROUP" -m 0750 \
  "$RELEASES_DIR" "$APP_ROOT/scripts" "$BACKUP_DIR"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0750 "$DATA_DIR" "$PROJECTS_DIR"
touch "$DEPLOY_LOG"
chown root:"$APP_GROUP" "$DEPLOY_LOG"
chmod 0640 "$DEPLOY_LOG"

release="$RELEASES_DIR/$commit"
previous_target=""
previous_commit=""
if [[ -L "$CURRENT_LINK" ]]; then
  previous_target="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
  previous_commit="$(basename "$previous_target")"
fi
started_at="$(date --iso-8601=seconds)"
initial_database=0
[[ ! -f "$DATA_DIR/app.db" ]] && initial_database=1

temp_release="$RELEASES_DIR/.${commit}.tmp.$$"
smoke_dir=""
smoke_pid=""
unit_backup=""
unit_existed=0
unit_staged=0
nginx_backup=""
nginx_existed=0
nginx_staged=0
current_switch_started=0
service_restart_attempted=0
deployment_committed=0

rollback_deployment() {
  if [[ "$current_switch_started" == 1 ]]; then
    if [[ -n "$previous_target" && -d "$previous_target" ]]; then
      rollback_link="$APP_ROOT/.current.rollback.$$"
      ln -s "$previous_target" "$rollback_link"
      mv -Tf "$rollback_link" "$CURRENT_LINK"
    else
      rm -f -- "$CURRENT_LINK"
    fi
  fi

  if [[ "$unit_staged" == 1 ]]; then
    if [[ "$unit_existed" == 1 && -f "$unit_backup" ]]; then
      install -o root -g root -m 0644 "$unit_backup" "$SYSTEMD_UNIT"
    else
      rm -f -- "$SYSTEMD_UNIT"
    fi
    systemctl daemon-reload || true
  fi

  if [[ "$service_restart_attempted" == 1 ]]; then
    if [[ -n "$previous_target" && -d "$previous_target" ]]; then
      systemctl restart "$SERVICE_NAME" || true
    else
      systemctl stop "$SERVICE_NAME" || true
    fi
  fi

  if [[ "$nginx_staged" == 1 ]]; then
    if [[ "$nginx_existed" == 1 && -f "$nginx_backup" ]]; then
      install -o root -g root -m 0644 "$nginx_backup" "$NGINX_SNIPPET"
    else
      rm -f -- "$NGINX_SNIPPET"
    fi
    nginx -t && systemctl reload nginx || true
  fi
}

cleanup() {
  rc=$?
  trap - EXIT
  set +e
  if [[ -n "$smoke_pid" ]]; then
    kill "$smoke_pid" 2>/dev/null || true
    wait "$smoke_pid" 2>/dev/null || true
  fi
  if [[ "$deployment_committed" != 1 ]]; then
    rollback_deployment
  fi
  [[ -n "$smoke_dir" && -d "$smoke_dir" ]] && rm -rf -- "$smoke_dir"
  [[ -d "$temp_release" ]] && rm -rf -- "$temp_release"
  [[ -n "$unit_backup" && -f "$unit_backup" ]] && rm -f -- "$unit_backup"
  [[ -n "$nginx_backup" && -f "$nginx_backup" ]] && rm -f -- "$nginx_backup"
  exit "$rc"
}
trap 'exit 130' INT
trap 'exit 143' TERM
trap cleanup EXIT

assert_no_running_jobs() {
  [[ ! -f "$DATA_DIR/app.db" ]] && return 0
  running="$(python3 - "$DATA_DIR/app.db" <<'PY'
import sqlite3
import sys

try:
    connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    row = connection.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'running'"
    ).fetchone()
    print(int(row[0]))
except sqlite3.Error:
    print(0)
finally:
    try:
        connection.close()
    except NameError:
        pass
PY
)"
  if [[ "$running" != 0 ]]; then
    echo "拒绝部署：当前有 $running 个生成任务正在运行，避免重启后重复执行和计费" >&2
    exit 6
  fi
}

assert_no_running_jobs

if [[ -f "$DATA_DIR/app.db" ]]; then
  backup="$BACKUP_DIR/app-before-${commit:0:12}-$(date -u '+%Y%m%d-%H%M%S').db"
  python3 - "$DATA_DIR/app.db" "$backup" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
destination = sqlite3.connect(sys.argv[2])
try:
    source.backup(destination)
finally:
    destination.close()
    source.close()
PY
  chown root:"$APP_GROUP" "$backup"
  chmod 0640 "$backup"
fi

if [[ ! -f "$release/.release-ready" ]]; then
  [[ -e "$release" ]] && rm -rf -- "$release"
  rm -rf -- "$temp_release"
  install -d -o root -g "$APP_GROUP" -m 0750 "$temp_release"
  git --git-dir="$GIT_DIR" archive "$commit" | tar -x -C "$temp_release"

  venv_dir="$temp_release/.venv"
  python3 -m venv "$venv_dir"
  "$venv_dir/bin/python" -m pip install --disable-pip-version-check \
    --requirement "$temp_release/requirements.txt"
  "$venv_dir/bin/python" -m pip check
  "$venv_dir/bin/python" -m compileall -q \
    "$temp_release/app.py" "$temp_release/config.py" "$temp_release/db.py" \
    "$temp_release/jobs.py" "$temp_release/qa.py" "$temp_release/runner.py" \
    "$temp_release/runner_agent.py"
  (
    cd "$temp_release"
    "$venv_dir/bin/python" -m unittest discover -s tests
  )

  smoke_dir="$(mktemp -d /tmp/ppt-web-release-smoke.XXXXXX)"
  env PPT_DATA_DIR="$smoke_dir/data" MOCK_MODE=force \
    PPT_ADMIN_PASSWORD=release-smoke-password \
    "$venv_dir/bin/python" "$temp_release/migrate_v2.py" >/dev/null
  (
    cd "$temp_release"
    exec env PPT_DATA_DIR="$smoke_dir/data" MOCK_MODE=force PORT=39080 \
      PPT_BASE_PATH= PPT_PYTHON_BIN="$venv_dir/bin/python" \
      "$venv_dir/bin/python" app.py >"$smoke_dir/server.log" 2>&1
  ) &
  smoke_pid=$!
  smoke_ok=no
  for _ in $(seq 1 60); do
    if curl -fsS http://127.0.0.1:39080/api/health >/dev/null; then
      smoke_ok=yes
      break
    fi
    sleep 0.5
  done
  if [[ "$smoke_ok" != yes ]]; then
    sed -n '1,160p' "$smoke_dir/server.log" >&2
    echo "release 启动测试失败" >&2
    exit 6
  fi
  kill "$smoke_pid" 2>/dev/null || true
  wait "$smoke_pid" 2>/dev/null || true
  smoke_pid=""
  rm -rf -- "$smoke_dir"
  smoke_dir=""

  rm -rf -- "$temp_release/engine/projects"
  printf '%s\n' "$commit" >"$temp_release/.deployed-commit"
  touch "$temp_release/.release-ready"
  chown -R root:"$APP_GROUP" "$temp_release"
  chmod -R g+rX,o-rwx "$temp_release"
  ln -s "$PROJECTS_DIR" "$temp_release/engine/projects"
  chown -h root:"$APP_GROUP" "$temp_release/engine/projects"
  mv "$temp_release" "$release"
fi

admin_username="$(sed -n 's/^PPT_ADMIN_USERNAME=//p' "$ENV_FILE" 2>/dev/null | tail -1)"
admin_password="$(sed -n 's/^PPT_ADMIN_PASSWORD=//p' "$ENV_FILE" 2>/dev/null | tail -1)"
migration_env=(env "PPT_DATA_DIR=$DATA_DIR")
[[ -n "$admin_username" ]] && migration_env+=("PPT_ADMIN_USERNAME=$admin_username")
[[ -n "$admin_password" ]] && migration_env+=("PPT_ADMIN_PASSWORD=$admin_password")
if [[ "$initial_database" == 1 ]]; then
  "${migration_env[@]}" "$release/.venv/bin/python" "$release/migrate_v2.py"
  if [[ -n "$admin_password" ]]; then
    sed -i '/^PPT_ADMIN_PASSWORD=/d' "$ENV_FILE"
    admin_password=""
  fi
else
  "${migration_env[@]}" "$release/.venv/bin/python" "$release/migrate_v2.py" schema
fi
chown -R "$APP_USER:$APP_GROUP" "$DATA_DIR" "$PROJECTS_DIR"
chmod -R u+rwX,g+rX,o-rwx "$DATA_DIR" "$PROJECTS_DIR"

install -o root -g "$APP_GROUP" -m 0750 \
  "$release/ops/server-deploy.sh" "$DEPLOY_SCRIPT.next"
mv -f "$DEPLOY_SCRIPT.next" "$DEPLOY_SCRIPT"
install -o root -g "$APP_GROUP" -m 0750 \
  "$release/ops/backup.sh" "$BACKUP_SCRIPT.next"
mv -f "$BACKUP_SCRIPT.next" "$BACKUP_SCRIPT"

if [[ -f "$SYSTEMD_UNIT" ]]; then
  unit_backup="$(mktemp /tmp/ppt-web.service.before.XXXXXX)"
  cp "$SYSTEMD_UNIT" "$unit_backup"
  unit_existed=1
fi
unit_staged=1
install -o root -g root -m 0644 \
  "$release/ops/ppt-web.service" "$SYSTEMD_UNIT"
install -o root -g root -m 0644 \
  "$release/ops/ppt-web-backup.service" /etc/systemd/system/ppt-web-backup.service
install -o root -g root -m 0644 \
  "$release/ops/ppt-web-backup.timer" /etc/systemd/system/ppt-web-backup.timer

nginx_backup="$(mktemp /tmp/nginx-ppt-generator.before.XXXXXX)"
if [[ -f "$NGINX_SNIPPET" ]]; then
  cp "$NGINX_SNIPPET" "$nginx_backup"
  nginx_existed=1
fi
nginx_staged=1
install -o root -g root -m 0644 \
  "$release/ops/nginx-ppt-web.conf" "$NGINX_SNIPPET.next"
mv -f "$NGINX_SNIPPET.next" "$NGINX_SNIPPET"
if ! nginx -t; then
  echo "Nginx 配置校验失败，开始自动回滚" >&2
  exit 6
fi

systemctl daemon-reload
systemctl enable ppt-web-backup.timer >/dev/null
systemctl start ppt-web-backup.timer

assert_no_running_jobs

next_link="$APP_ROOT/.current.$$"
ln -s "$release" "$next_link"
current_switch_started=1
mv -Tf "$next_link" "$CURRENT_LINK"
healthy=no
service_restart_attempted=1
if systemctl restart "$SERVICE_NAME"; then
  for _ in $(seq 1 90); do
    if curl -fsS "$HEALTH_URL" >/dev/null; then
      healthy=yes
      break
    fi
    sleep 1
  done
fi

if [[ "$healthy" == yes ]] && ! systemctl reload nginx; then
  echo "Nginx 重载失败，开始回滚" >&2
  healthy=no
fi

if [[ "$healthy" != yes ]]; then
  echo "新版本健康检查失败，开始自动回滚" >&2
  printf '%s\t%s\t%s\tfailed\thealth-check\n' \
    "$started_at" "$commit" "$previous_commit" >>"$DEPLOY_LOG"
  exit 7
fi

service_state="$(systemctl is-active "$SERVICE_NAME")"
restart_count="$(systemctl show "$SERVICE_NAME" -p NRestarts --value)"
printf '%s\t%s\t%s\tsuccess\t%s\n' \
  "$started_at" "$commit" "$previous_commit" "$service_state" >>"$DEPLOY_LOG"
deployment_committed=1

echo "deploy_status=success"
echo "commit=$commit"
echo "previous_commit=$previous_commit"
echo "release=$release"
echo "service=$service_state"
echo "restart_count=$restart_count"
echo "health=$HEALTH_URL"
