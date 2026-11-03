#!/data/data/com.termux/files/usr/bin/bash
# Pi Pad one-command setup for Termux on the Raspberry Pi (LineageOS).
# Usage: bash pi/setup.sh localhost:5555
set -e
SERIAL="${1:-localhost:5555}"
DIR="$(cd "$(dirname "$0")" && pwd)"
pkg update -y
pkg install -y python android-tools cloudflared
mkdir -p ~/.pipad ~/.termux/boot
[ -f ~/.pipad/token ] || python -c "import secrets;print(secrets.token_urlsafe(24))" > ~/.pipad/token
echo "$SERIAL" > ~/.pipad/serial
cat > ~/.pipad/run.sh <<RUN
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
pkill -f "$DIR/server.py" 2>/dev/null; pkill cloudflared 2>/dev/null; sleep 1
adb connect \$(cat ~/.pipad/serial) >/dev/null 2>&1
PI_TOKEN=\$(cat ~/.pipad/token) ADB_SERIAL=\$(cat ~/.pipad/serial) nohup python "$DIR/server.py" > ~/.pipad/server.log 2>&1 &
: > ~/.pipad/tunnel.log
nohup cloudflared tunnel --url http://localhost:8080 > ~/.pipad/tunnel.log 2>&1 &
for i in \$(seq 1 40); do U=\$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' ~/.pipad/tunnel.log | head -1); [ -n "\$U" ] && break; sleep 1; done
echo ""; echo "Pi address:   \$U"; echo "Access token: \$(cat ~/.pipad/token)"
RUN
chmod +x ~/.pipad/run.sh
cp ~/.pipad/run.sh ~/.termux/boot/pipad && chmod +x ~/.termux/boot/pipad
~/.pipad/run.sh
