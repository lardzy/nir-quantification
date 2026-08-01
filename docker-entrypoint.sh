#!/bin/sh
set -eu

run_uid="${NIRQ_RUN_UID:-10001}"
run_gid="${NIRQ_RUN_GID:-10001}"
db_path="${NIRQ_DB_PATH:-/data/spectra.sqlite3}"
export_roots="${EXPORT_ROOTS:-/workspace/exports}"

case "$run_uid" in
  ''|*[!0-9]*)
    echo "NIRQ_RUN_UID must be a numeric user id" >&2
    exit 64
    ;;
esac
case "$run_gid" in
  ''|*[!0-9]*)
    echo "NIRQ_RUN_GID must be a numeric group id" >&2
    exit 64
    ;;
esac
if [ "$run_uid" -eq 0 ] || [ "$run_gid" -eq 0 ]; then
  echo "NIRQ_RUN_UID and NIRQ_RUN_GID must be non-zero" >&2
  exit 64
fi

prepare_writable_path() {
  target_path="$1"
  mkdir -p "$target_path"
  if ! chown -R "$run_uid:$run_gid" "$target_path"; then
    echo "failed to make $target_path writable by $run_uid:$run_gid" >&2
    echo "remove any Compose user override or repair the host mount ownership" >&2
    exit 73
  fi
}

check_writable_path() {
  target_path="$1"
  if [ ! -d "$target_path" ] || [ ! -w "$target_path" ]; then
    echo "$target_path is not writable by uid=$(id -u) gid=$(id -g)" >&2
    echo "run the container entrypoint as root so it can repair bind-mount ownership" >&2
    exit 73
  fi
}

db_dir=$(dirname "$db_path")
case "$db_dir" in
  /data|/data/*) ;;
  *)
    echo "refusing to change ownership outside /data: $db_dir" >&2
    exit 64
    ;;
esac

old_ifs=$IFS
IFS=:
for export_root in $export_roots; do
  case "$export_root" in
    /workspace/exports|/workspace/exports/*) ;;
    *)
      echo "refusing to change ownership outside /workspace/exports: $export_root" >&2
      exit 64
      ;;
  esac
done
IFS=$old_ifs

if [ "$(id -u)" -eq 0 ]; then
  prepare_writable_path "$db_dir"
  old_ifs=$IFS
  IFS=:
  for export_root in $export_roots; do
    if [ -n "$export_root" ]; then
      prepare_writable_path "$export_root"
    fi
  done
  IFS=$old_ifs
  exec setpriv --reuid="$run_uid" --regid="$run_gid" --clear-groups "$@"
fi

check_writable_path "$db_dir"
old_ifs=$IFS
IFS=:
for export_root in $export_roots; do
  if [ -n "$export_root" ]; then
    check_writable_path "$export_root"
  fi
done
IFS=$old_ifs
exec "$@"
