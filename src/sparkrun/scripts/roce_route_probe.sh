#!/bin/bash
# Route-aware RoCEv2 GID probe. SPARKRUN_PEER_IPS is injected by Sparkrun.
set -uo pipefail

SYSFS_ROOT=${SPARKRUN_SYSFS_ROOT:-/sys}
[[ -n "${SPARKRUN_PEER_IPS:-}" ]] || exit 0

# Resolve the local source address selected by Linux for each peer, map its
# interface to the RDMA HCA, then require the exact IPv4-mapped RoCEv2 GID.
while IFS= read -r peer_ip; do
    [[ -n "$peer_ip" ]] || continue
    route=$(ip -o -4 route get "$peer_ip" 2>/dev/null | head -n 1 || true)
    [[ -n "$route" ]] || continue

    dev=$(awk '{for (i=1; i<=NF; i++) if ($i == "dev") {print $(i+1); exit}}' <<< "$route")
    src=$(awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}' <<< "$route")
    [[ -n "$dev" && -n "$src" && "$dev" != lo ]] || continue

    hca_root="$SYSFS_ROOT/class/net/$dev/device/infiniband"
    [[ -d "$hca_root" ]] || continue
    for hca_path in "$hca_root"/*; do
        [[ -e "$hca_path" ]] || continue
        hca=${hca_path##*/}
        IFS=. read -r oct1 oct2 oct3 oct4 <<< "$src"
        [[ -n "${oct4:-}" ]] || continue
        want_gid=$(printf '0000:0000:0000:0000:0000:ffff:%02x%02x:%02x%02x' "$oct1" "$oct2" "$oct3" "$oct4")

        for port_dir in "$SYSFS_ROOT/class/infiniband/$hca/ports"/*; do
            [[ -d "$port_dir" ]] || continue
            port=${port_dir##*/}
            [[ "$port" =~ ^[0-9]+$ ]] || continue
            gid_dir="$port_dir/gids"
            type_dir="$port_dir/gid_attrs/types"
            [[ -d "$gid_dir" && -d "$type_dir" ]] || continue
            for gid_file in "$gid_dir"/*; do
                [[ -f "$gid_file" ]] || continue
                gid_index=${gid_file##*/}
                [[ "$gid_index" =~ ^[0-9]+$ ]] || continue
                type_file="$type_dir/$gid_index"
                [[ -r "$type_file" ]] || continue
                gid=$(tr '[:upper:]' '[:lower:]' < "$gid_file" 2>/dev/null | tr -d '\r' || true)
                gid_type=$(tr -d '\r' < "$type_file" 2>/dev/null || true)
                [[ "$gid" == "$want_gid" ]] || continue
                [[ "$gid_type" == *"RoCE v2"* || "$gid_type" == *"RoCEv2"* ]] || continue
                ndev_file="$port_dir/gid_attrs/ndevs/$gid_index"
                if [[ -r "$ndev_file" ]]; then
                    gid_dev=$(tr -d '\r' < "$ndev_file" 2>/dev/null || true)
                    [[ "$gid_dev" == "$dev" ]] || continue
                fi
                printf 'ROCE_ROUTE=%s|%s|%s|%s|%s|%s|%s\n' "$peer_ip" "$src" "$dev" "$hca" "$port" "$gid_index" "$gid"
            done
        done
    done
done < <(tr ',' '\n' <<< "$SPARKRUN_PEER_IPS")

# An unmatched candidate is normal; keep earlier route rows and return success.
exit 0
