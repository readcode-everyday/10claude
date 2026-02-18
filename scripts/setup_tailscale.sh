#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_tailscale.sh  —  Install Tailscale and connect this machine to your
#                         private mesh network so you can reach OpenClaw from
#                         any device (phone, laptop, etc.) without port-forwarding.
#
# Usage:
#   bash scripts/setup_tailscale.sh
#
# After running, open https://<tailscale-ip> on your phone (once Tailscale is
# installed there too) to reach the OpenClaw dashboard.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

echo "==> Installing Tailscale..."
curl -fsSL https://tailscale.com/install.sh | sh

echo ""
echo "==> Starting Tailscale and authenticating..."
echo "    A browser link will appear — open it to log in (Google / GitHub / email)."
echo "    If this is a headless server, use: sudo tailscale up --auth-key=<key>"
echo "    (Generate an auth key at https://login.tailscale.com/admin/settings/keys)"
echo ""

sudo tailscale up

echo ""
echo "==> Tailscale is connected."
echo "    Your Tailscale IP:"
tailscale ip -4

echo ""
echo "─────────────────────────────────────────────────────────────────────────"
echo "  Next steps to access OpenClaw on your phone:"
echo ""
echo "  1. Install Tailscale on your phone:"
echo "       iOS:     https://apps.apple.com/app/tailscale/id1470499037"
echo "       Android: https://play.google.com/store/apps/details?id=com.tailscale.ipn.android"
echo ""
echo "  2. Sign in with the same Tailscale account."
echo ""
echo "  3. Open your browser and go to:"
echo "       https://$(tailscale ip -4)"
echo "       (accept the self-signed cert warning once)"
echo ""
echo "  4. Log in with:"
echo "       Username: admin"
echo "       Password: (see UI_PASS_HASH in .env — change it!)"
echo ""
echo "  Optional — use a nicer hostname instead of an IP:"
echo "    Enable MagicDNS in your Tailscale admin panel:"
echo "    https://login.tailscale.com/admin/dns"
echo "    Then access OpenClaw at:  https://<your-machine-name>.<tailnet>.ts.net"
echo "─────────────────────────────────────────────────────────────────────────"
