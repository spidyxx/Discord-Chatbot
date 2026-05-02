#!/bin/bash
set -e

UNRAID="root@192.168.178.70"
REMOTE_DIR="/mnt/user/appdata/Discord Chatbot"

echo "Syncing files..."
rsync -av --delete --exclude='.git' --exclude='*.pyc' --exclude='data/' --exclude='logs/' --exclude='.env' \
  ./ "$UNRAID:$REMOTE_DIR/"

echo "Rebuilding container..."
ssh "$UNRAID" "
  cd '/mnt/user/appdata/Discord Chatbot/' &&
  docker build -t discord_chatbot:latest . &&
  docker stop discord_chatbot &&
  docker rm discord_chatbot &&
  docker run -d \
    --name discord_chatbot \
    --restart unless-stopped \
    --env-file .env \
    -e TZ=\$(grep '^TIMEZONE=' .env | cut -d= -f2 || echo 'Europe/Berlin') \
    --user 99:100 \
    -v '/mnt/user/appdata/Discord Chatbot/data:/app/data' \
    -v '/mnt/user/appdata/Discord Chatbot/logs:/app/logs' \
    discord_chatbot:latest
"

echo "Done."
