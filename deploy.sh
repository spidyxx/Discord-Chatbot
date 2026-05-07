#!/bin/bash
set -e

UNRAID="root@192.168.178.70"

deploy() {
  local NAME="$1"        # appdata folder, e.g. Discord_Chatbot or Discord_Chatbot_2
  local CONTAINER="$2"   # docker container/image name, e.g. discord_chatbot or discord_chatbot_2
  local REMOTE_DIR="/mnt/user/appdata/$NAME"
  local DATA_VOL="/mnt/cache/appdata/$NAME"

  echo
  echo "=== Deploying $NAME → $CONTAINER ==="

  echo "Syncing files..."
  rsync -av --delete \
    --exclude='.git' --exclude='*.pyc' --exclude='data/' --exclude='logs/' --exclude='.env' \
    --exclude='plugins/**/*.cfg' \
    --exclude='statuses.txt' \
    ./ "$UNRAID:$REMOTE_DIR/"
  # Plugin configs and statuses.txt are first-seeded then preserved across deploys
  # so per-bot customisations survive.
  rsync -av --ignore-existing plugins/core/*.cfg "$UNRAID:$REMOTE_DIR/plugins/core/"
  rsync -av --ignore-existing statuses.txt "$UNRAID:$REMOTE_DIR/"

  echo "Rebuilding container..."
  ssh "$UNRAID" "
    cd '$REMOTE_DIR' &&
    docker build -t ${CONTAINER}:latest . &&
    docker stop ${CONTAINER} 2>/dev/null || true &&
    docker rm ${CONTAINER} 2>/dev/null || true &&
    docker run -d \
      --name ${CONTAINER} \
      --restart unless-stopped \
      --env-file .env \
      -e TZ=\$(grep '^TIMEZONE=' .env | cut -d= -f2 || echo 'Europe/Berlin') \
      --user 99:100 \
      -v '${DATA_VOL}:${DATA_VOL}' \
      ${CONTAINER}:latest
  "
}

TARGET="${1:-both}"
case "$TARGET" in
  marvin)
    deploy "Discord_Chatbot"   "discord_chatbot"
    ;;
  snoop)
    deploy "Discord_Chatbot_2" "discord_chatbot_2"
    ;;
  both)
    deploy "Discord_Chatbot"   "discord_chatbot"
    deploy "Discord_Chatbot_2" "discord_chatbot_2"
    ;;
  *)
    echo "usage: $0 [marvin|snoop|both]   (default: both)" >&2
    exit 1
    ;;
esac

echo
echo "Done."
