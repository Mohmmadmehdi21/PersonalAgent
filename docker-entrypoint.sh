#!/bin/sh
set -eu

# Render mounts Docker secret files under /etc/secrets at runtime. The current
# application reads its OAuth files from the project root, so copy them there
# when present. The bot itself can still start without OAuth; Prepare will give
# its existing configuration error until the required files are supplied.
for name in client_secret.json token.json; do
    source_path="/etc/secrets/$name"
    target_path="/app/$name"
    if [ -f "$source_path" ]; then
        cp "$source_path" "$target_path"
    fi
done

exec python main.py telegram-bot
