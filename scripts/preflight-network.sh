#!/usr/bin/env bash
# Pre-flight network check for the Sengled hub from Ubuntu/Linux.
# Usage: ./scripts/preflight-network.sh [HUB_IP]
set -uo pipefail

HUB="${1:-10.42.0.119}"
LOGDIR="$(cd "$(dirname "$0")/.." && pwd)/output"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/preflight-$(date +%Y%m%d-%H%M%S).log"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

port_open() { timeout 6 bash -c "exec 3<>/dev/tcp/$HUB/$1" >/dev/null 2>&1; }

log "Sengled hub pre-flight check for $HUB"

if ping -c 1 -W 2 "$HUB" >/dev/null 2>&1; then
  log "PING       : UP"
else
  log "PING       : DOWN -- hub not reachable, check power/LAN"
fi

if port_open 8686; then
  log "TCP/8686   : OPEN  (stock backdoor -- required by reclaim tool)"
else
  log "TCP/8686   : closed (reclaim tool cannot start telnet -- BLOCKER)"
fi

if port_open 23; then
  log "TCP/23     : OPEN  (telnet already running)"
else
  log "TCP/23     : closed (normal pre-reclaim; tool starts telnetd via 8686)"
fi

if port_open 6638; then
  log "TCP/6638   : OPEN  (EZSP gateway -- hub already reclaimed)"
else
  log "TCP/6638   : closed (normal pre-reclaim; appears only after reclaim)"
fi

log ""
log "Summary written to: $LOG"
log "8686 open + ping up => hub is ready for the reclaim step."
