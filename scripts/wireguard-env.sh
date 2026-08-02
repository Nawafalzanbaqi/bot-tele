#!/usr/bin/env sh
# ---------------------------------------------------------------------------
# Turn a WireGuard .conf into the .env lines the `vpn` service needs.
#
# Providers hand out a file; this stack reads environment variables. Doing that
# translation by hand means copying a private key between two windows and
# discovering the typo later as "the VPN will not start" - so it is done here
# instead, once, by something that cannot mistype.
#
# Usage:
#     ./scripts/wireguard-env.sh ~/wg0.conf >> .env
#     chmod 600 .env
#
# **This prints a private key to standard output.** Redirect it to .env, do not
# paste the result into a chat window, and do not commit the file.
#
# POSIX sh and awk only: no Python, no jq, nothing to install on the device.
# ---------------------------------------------------------------------------
set -eu

CONF="${1:-}"

if [ -z "${CONF}" ] || [ ! -f "${CONF}" ]; then
    printf 'usage: %s <wireguard.conf>\n' "$0" >&2
    exit 2
fi

# Read one `key = value` from a section, tolerating any spacing a provider uses.
field() {
    awk -v section="$1" -v key="$2" '
        /^[[:space:]]*\[/ {
            current = $0
            gsub(/[][[:space:]]/, "", current)
            next
        }
        {
            line = $0
            sub(/#.*$/, "", line)
            split(line, parts, "=")
            name = parts[1]
            gsub(/[[:space:]]/, "", name)
            if (tolower(current) == tolower(section) && tolower(name) == tolower(key)) {
                value = substr(line, index(line, "=") + 1)
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
                print value
                exit
            }
        }
    ' "${CONF}"
}

PRIVATE_KEY="$(field Interface PrivateKey)"
ADDRESSES="$(field Interface Address)"
PUBLIC_KEY="$(field Peer PublicKey)"
PRESHARED_KEY="$(field Peer PresharedKey)"
ENDPOINT="$(field Peer Endpoint)"

for pair in "PrivateKey:${PRIVATE_KEY}" "Address:${ADDRESSES}" \
            "PublicKey:${PUBLIC_KEY}" "Endpoint:${ENDPOINT}"; do
    name="${pair%%:*}"
    value="${pair#*:}"
    if [ -z "${value}" ]; then
        printf 'error: %s is missing from %s\n' "${name}" "${CONF}" >&2
        exit 1
    fi
done

# `host:port`, where the host may be a name. Gluetun wants an IP, so a name is
# resolved here - and failing loudly beats a container that restarts forever.
ENDPOINT_HOST="${ENDPOINT%:*}"
ENDPOINT_PORT="${ENDPOINT##*:}"

case "${ENDPOINT_HOST}" in
    *[!0-9.]*)
        RESOLVED="$(getent ahostsv4 "${ENDPOINT_HOST}" 2>/dev/null | awk 'NR==1 {print $1}')"
        if [ -z "${RESOLVED}" ]; then
            printf 'error: could not resolve endpoint host %s\n' "${ENDPOINT_HOST}" >&2
            exit 1
        fi
        printf '# %s resolved to %s\n' "${ENDPOINT_HOST}" "${RESOLVED}"
        ENDPOINT_HOST="${RESOLVED}"
        ;;
esac

cat <<ENV
# --- WireGuard egress, generated from $(basename "${CONF}") ---
VPN_SERVICE_PROVIDER=custom
VPN_TYPE=wireguard
VPN_ENDPOINT_IP=${ENDPOINT_HOST}
VPN_ENDPOINT_PORT=${ENDPOINT_PORT}
WIREGUARD_PRIVATE_KEY=${PRIVATE_KEY}
WIREGUARD_PUBLIC_KEY=${PUBLIC_KEY}
WIREGUARD_PRESHARED_KEY=${PRESHARED_KEY}
WIREGUARD_ADDRESSES=${ADDRESSES}
# Only the download engine uses this; delivery stays on the direct route.
MEDIAHUB_DOWNLOAD__PROXY=http://vpn:8888
ENV
