#!/bin/bash
# InfiniBand/RDMA detection for DGX Spark
# Outputs key=value pairs on stdout for NCCL configuration.
# Diagnostic messages go to stderr so they don't pollute parsing.
#
# Based on the proven detection in scripts/infiniband-detect.sh.

set -uo pipefail

# sparkrun:include _mgmt_iface.sh

# sparkrun:include _gid_index.sh

echo "Running InfiniBand detection..." >&2

if ! [ -d /sys/class/infiniband ]; then
    echo "IB_DETECTED=0"
    exit 0
fi

ACTIVE_HCAS=()
ACTIVE_NETIFS=()
UCX_DEVS=()
GID_INDEX=""

# Iterate over InfiniBand devices in sysfs
for ib_path in /sys/class/infiniband/*; do
    [ -e "$ib_path" ] || continue
    hca_name=$(basename "$ib_path")

    # Inspect every port; multi-port HCAs can have a usable fabric only on
    # port 2 (or later), even when port 1 is down.
    for port_path in "$ib_path"/ports/*; do
        [ -d "$port_path" ] || continue
        port_num=$(basename "$port_path")
        state_file="$port_path/state"
        [ -f "$state_file" ] || continue
        state_val=$(cat "$state_file" 2>/dev/null)

        if [[ "$state_val" == *"ACTIVE"* ]]; then
            ACTIVE_HCAS+=("$hca_name:$port_num")
            UCX_DEVS+=("${hca_name}:${port_num}")

            # Determine GID Index (run once on first active port). Always
            # yields an index; -1 hands the choice to NCCL.
            if [ -z "$GID_INDEX" ]; then
                GID_INDEX=$(sparkrun_rocev2_gid_index "$hca_name" "$ib_path" "$port_num")
            else
                echo "Device $hca_name port $port_num: Active" >&2
            fi

            # Find Ethernet interface backing this device, without duplicates.
            net_dir="$ib_path/device/net"
            if [ -d "$net_dir" ]; then
                net_if=$(ls "$net_dir" | head -n 1)
                if [ -n "$net_if" ] && [[ " ${ACTIVE_NETIFS[*]} " != *" $net_if "* ]]; then
                    ACTIVE_NETIFS+=("$net_if")
                fi
            fi
        fi
    done
done

if [ ${#ACTIVE_HCAS[@]} -eq 0 ]; then
    echo "No active RDMA devices found." >&2
    echo "IB_DETECTED=0"
    exit 0
fi

# Build comma-separated lists
HCA_LIST=$(IFS=,; echo "${ACTIVE_HCAS[*]}")
NET_LIST=$(IFS=,; echo "${ACTIVE_NETIFS[*]}")
UCX_LIST=$(IFS=,; echo "${UCX_DEVS[*]}")

# Detect the management network interface and its IP.  Computed *after*
# NET_LIST so the fabric adapters can be excluded from the heuristic scan --
# they are not management interfaces.  May legitimately come back empty on a
# host with no management network we can identify; see _mgmt_iface.sh.
DEFAULT_IF=$(sparkrun_mgmt_iface "$NET_LIST")
MGMT_IP=""
if [ -n "$DEFAULT_IF" ]; then
    MGMT_IP=$(ip -4 addr show "$DEFAULT_IF" 2>/dev/null | grep -oP 'inet \K[0-9.]+' | head -1)
fi

# Resolve IPv4 addresses of active IB network interfaces
IB_IPS=()
for net_if in "${ACTIVE_NETIFS[@]}"; do
    ip_addr=$(ip -4 addr show "$net_if" 2>/dev/null | grep -oP 'inet \K[0-9.]+' | head -1)
    if [ -n "$ip_addr" ]; then
        IB_IPS+=("$ip_addr")
    fi
done
IB_IP_LIST=$(IFS=,; echo "${IB_IPS[*]}")

echo "---------------------------------------------------" >&2
echo "Detection complete:" >&2
echo "  HCA_LIST   = $HCA_LIST" >&2
echo "  NET_LIST   = $NET_LIST" >&2
echo "  IB_IPS     = $IB_IP_LIST" >&2
echo "  GID_INDEX  = $GID_INDEX" >&2
echo "  DEFAULT_IF = $DEFAULT_IF" >&2
echo "  MGMT_IP    = $MGMT_IP" >&2
echo "---------------------------------------------------" >&2

# Output key=value pairs on stdout (parsed by sparkrun)
echo "IB_DETECTED=1"
echo "DETECTED_GID_INDEX=$GID_INDEX"
echo "DETECTED_HCA_LIST=$HCA_LIST"
echo "DETECTED_HCA_LIST_EXACT=1"
echo "DETECTED_SOCKET_IFNAME=$DEFAULT_IF"
echo "DETECTED_NET_LIST=$NET_LIST"
echo "DETECTED_UCX_LIST=$UCX_LIST"
echo "DETECTED_IB_IPS=$IB_IP_LIST"
echo "DETECTED_MGMT_IP=$MGMT_IP"
