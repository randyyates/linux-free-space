#!/bin/sh
# Install for the current user: `freespace` command + app-menu entry.
set -e
cd "$(dirname "$0")"
install -Dm755 freespace.py "$HOME/.local/bin/freespace"
install -Dm644 freespace.desktop "$HOME/.local/share/applications/freespace.desktop"
echo "Installed. Run 'freespace [PATH]' or find 'Free Space' in your app menu."
