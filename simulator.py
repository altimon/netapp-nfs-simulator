#!/usr/bin/env python3
"""
NetApp ONTAP SSH Simulator
Presents a realistic cluster1::> prompt over SSH (asyncssh).
State is persisted to state.json.
"""

import asyncio
import json
import os
import re
import sys
import shutil
import argparse
from pathlib import Path
from textwrap import dedent

try:
    import asyncssh
except ImportError:
    print("asyncssh not installed. Run: pip3 install asyncssh", file=sys.stderr)
    sys.exit(1)

STATE_PATH = Path(os.environ.get("NETAPP_STATE", "/opt/netapp-sim/state.json"))
DEFAULT_STATE_PATH = Path(os.environ.get("NETAPP_DEFAULT_STATE", str(Path(__file__).parent / "state.default.json")))
HOST_KEY_PATH = Path(os.environ.get("NETAPP_HOST_KEY", "/opt/netapp-sim/host_key"))
LISTEN_PORT = int(os.environ.get("NETAPP_PORT", "2223"))
LISTEN_HOST = os.environ.get("NETAPP_HOST", "0.0.0.0")
SSH_USER = os.environ.get("NETAPP_USER", "admin")
SSH_PASS = os.environ.get("NETAPP_PASS", "netapp123")

# ── state helpers ────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        with STATE_PATH.open() as f:
            return json.load(f)
    if DEFAULT_STATE_PATH.exists():
        with DEFAULT_STATE_PATH.open() as f:
            return json.load(f)
    raise FileNotFoundError(f"No state.json found at {STATE_PATH} or {DEFAULT_STATE_PATH}")

def save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("w") as f:
        json.dump(state, f, indent=2)

def reset_state():
    if DEFAULT_STATE_PATH.exists():
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(DEFAULT_STATE_PATH, STATE_PATH)
        print(f"State reset from {DEFAULT_STATE_PATH} → {STATE_PATH}")
    else:
        print("Default state.json not found.", file=sys.stderr)
        sys.exit(1)

# ── output formatting helpers ────────────────────────────────────────────────

def col(value: str, width: int) -> str:
    return str(value).ljust(width)

def hr(char="-", width=80) -> str:
    return char * width

# ── command handlers ─────────────────────────────────────────────────────────

def cmd_version(state, _args):
    v = state["cluster"]["version"]
    return f"\n{v}\n"

def cmd_system_node_show(state, _args):
    lines = [
        "",
        "                                            Display  Failover  VIA",
        "Node                 Health  Eligibility   Level    Priority  Count",
        "-------------------- ------- ------------- -------- --------- -----",
    ]
    for n in state["nodes"]:
        health = n.get("health", "true")
        lines.append(
            f"{n['name']:<20} {'true':<7} {'true':<13} {'INFO':<8} {'Secondary':<9} {'0'}"
        )
    lines.append("")
    return "\n".join(lines)

def _aggr_rows(state):
    rows = []
    for a in state["aggregates"]:
        rows.append(
            f"{a['name']:<20} {a['node']:<26} {a['size']:<8} {a['used']:<8} {a['state']}"
        )
    return rows

def cmd_aggr_show(state, _args):
    lines = [
        "",
        "Aggregate     Size Available Used% State   #Vols  Nodes            RAID Status",
        "--------- -------- --------- ----- ------- ------ ---------------- -----------",
    ]
    for a in state["aggregates"]:
        lines.append(
            f"{a['name']:<9} {a['size']:>8} {'N/A':>9} {'N/A':>5} {a['state']:<7} {'N/A':<6} {a['node']:<16} {'raid_dp'}"
        )
    lines.append("")
    return "\n".join(lines)

def _volume_matches(vol, filters: dict) -> bool:
    for k, v in filters.items():
        fmap = {
            "vserver": "vserver",
            "volume": "name",
            "aggregate": "aggregate",
            "state": "state",
        }
        field = fmap.get(k)
        if field and vol.get(field) != v:
            return False
    return True

def cmd_volume_show(state, args):
    filters = _parse_flags(args)
    volumes = [v for v in state["volumes"] if _volume_matches(v, filters)]

    show_fields = None
    if "-fields" in args:
        idx = args.index("-fields")
        if idx + 1 < len(args):
            show_fields = [f.strip() for f in args[idx + 1].split(",")]

    if show_fields:
        header_map = {
            "vserver": ("Vserver", 20),
            "volume": ("Volume", 20),
            "size": ("Size", 8),
            "aggregate": ("Aggregate", 20),
            "state": ("State", 8),
            "type": ("Type", 6),
            "security-style": ("Security", 10),
            "junction-path": ("Junction Path", 20),
            "policy": ("Policy", 20),
        }
        hdrs = [header_map.get(f, (f, 16)) for f in show_fields]
        header_line = "".join(col(h, w) for h, w in hdrs)
        sep_line = " ".join("-" * w for _, w in hdrs)
        lines = ["", header_line, sep_line]
        for v in volumes:
            row = ""
            for f, (_, w) in zip(show_fields, hdrs):
                field_map = {
                    "vserver": v.get("vserver", ""),
                    "volume": v.get("name", ""),
                    "size": v.get("size", ""),
                    "aggregate": v.get("aggregate", ""),
                    "state": v.get("state", ""),
                    "type": v.get("type", ""),
                    "security-style": v.get("security_style", ""),
                    "junction-path": v.get("junction_path", ""),
                    "policy": v.get("export_policy", ""),
                }
                row += col(field_map.get(f, ""), w)
            lines.append(row)
        lines.append("")
        return "\n".join(lines)

    lines = [
        "",
        "Vserver   Volume       Aggregate    State      Type       Size  Available Used%",
        "--------- ------------ ------------ ---------- ---------- ----- --------- -----",
    ]
    for v in volumes:
        lines.append(
            f"{v['vserver']:<9} {v['name']:<12} {v['aggregate']:<12} "
            f"{v['state']:<10} {v['type']:<10} {v['size']:<5} {'N/A':<9} {'N/A'}"
        )
    lines.append(f"\n{len(volumes)} entries were displayed.")
    return "\n".join(lines)

def cmd_network_interface_show(state, args):
    filters = _parse_flags(args)
    lifs = state["lifs"]
    if "vserver" in filters:
        lifs = [l for l in lifs if l["vserver"] == filters["vserver"]]

    lines = [
        "",
        "            Logical    Status     Network            Current       Current Is",
        "Vserver     Interface  Admin/Oper Address/Mask       Node          Port    Home",
        "----------- ---------- ---------- ------------------ ------------- ------- ----",
    ]
    for l in lifs:
        addr_mask = f"{l['address']}/24"
        admin_oper = f"{l['status_admin']}/{l['status_oper']}"
        lines.append(
            f"{l['vserver']:<11} {l['name']:<26} {admin_oper:<10} "
            f"{addr_mask:<18} {l['home_node']:<13} {l['home_port']:<7} {'true'}"
        )
    lines.append(f"\n{len(lifs)} entries were displayed.")
    return "\n".join(lines)

def cmd_vserver_show(state, _args):
    lines = [
        "",
        "                               Admin    Operational  Root                Name    Name",
        "Vserver     Type    Subtype    State    State        Volume     Aggregate Service Mapping",
        "----------- ------- ---------- -------- ------------ ---------- --------- ------- -------",
    ]
    for svm in state["svms"]:
        lines.append(
            f"{svm['name']:<11} {'data':<7} {'default':<10} {svm['state']:<8} "
            f"{'running':<12} {'interview_test':<10} {'n1_SAS_900':<9} {'file':<7} {'file'}"
        )
    lines.append("")
    return "\n".join(lines)

def cmd_vserver_nfs_show(state, _args):
    lines = [
        "",
        "Vserver: vs_parn_interview",
        "  General NFS Access:            true",
        "  NFS v3:                        enabled",
        "  NFS v4.0:                      disabled",
        "  NFS v4.1:                      disabled",
        "  UDP Transport:                 enabled",
        "  TCP Transport:                 enabled",
        "",
    ]
    return "\n".join(lines)

def cmd_export_policy_show(state, _args):
    lines = ["", "Vserver          Policy Name", "---------------- ---------------"]
    for svm in state["svms"]:
        for policy in svm["export_policies"]:
            lines.append(f"{svm['name']:<16} {policy}")
    lines.append("")
    return "\n".join(lines)

_RULE_FIELD_MAP = {
    "vserver":      ("Vserver",      18, lambda svm, r: svm["name"]),
    "policyname":   ("Policy Name",  24, lambda svm, r: None),   # filled per-policy
    "ruleindex":    ("Rule Index",    8, lambda svm, r: str(r["rule_index"])),
    "protocol":     ("Protocol",      9, lambda svm, r: "nfs"),
    "clientmatch":  ("Client Match", 20, lambda svm, r: r.get("clientmatch", "")),
    "rorule":       ("Ro Rule",      10, lambda svm, r: r.get("ro_rule", "any")),
    "rwrule":       ("Rw Rule",      10, lambda svm, r: r.get("rw_rule", "never")),
    "superuser":    ("Super User",   12, lambda svm, r: r.get("super_user", "none")),
    "anon":         ("Anon",          8, lambda svm, r: r.get("anon", "65534")),
}

def cmd_export_policy_rule_show(state, args):
    filters = _parse_flags(args)

    # collect matching (svm, policy_name, rule) tuples
    rows = []
    for svm in state["svms"]:
        if "vserver" in filters and svm["name"] != filters["vserver"]:
            continue
        for policy_name, policy in svm["export_policies"].items():
            if "policyname" in filters and policy_name != filters["policyname"]:
                continue
            for rule in policy["rules"]:
                rows.append((svm, policy_name, rule))

    # -fields mode
    if "-fields" in args:
        idx = args.index("-fields")
        if idx + 1 < len(args):
            field_names = [f.strip().lower() for f in args[idx + 1].split(",")]
            all_cols = ["vserver", "policyname"] + field_names
            hdrs = []
            for f in all_cols:
                meta = _RULE_FIELD_MAP.get(f)
                if meta:
                    hdrs.append((f, meta[0], meta[1], meta[2]))
            header_line = "".join(col(h, w) for _, h, w, _ in hdrs)
            sep_line    = " ".join("-" * w for _, _, w, _ in hdrs)
            lines = ["", header_line, sep_line]
            for svm, policy_name, rule in rows:
                row = ""
                for f, _, w, getter in hdrs:
                    val = policy_name if f == "policyname" else getter(svm, rule)
                    row += col(val or "", w)
                lines.append(row)
            lines.append("")
            return "\n".join(lines)

    # default tabular mode
    lines = [
        "",
        "                                                            Vserver: vs_parn_interview",
        "                                          Policy            Rule    Access   Client              RO         RW         Super",
        "Vserver          Policy Name              Index   Protocol  Type    Match                Rule       Rule       User",
        "---------------- ------------------------ ------- --------- ------  ------------------- ---------- ---------- ----------",
    ]
    for svm, policy_name, rule in rows:
        lines.append(
            f"{svm['name']:<16} {policy_name:<24} {rule['rule_index']:<7} "
            f"{'nfs':<9} {'allow':<6}  {rule['clientmatch']:<19} {rule.get('ro_rule','any'):<10} "
            f"{rule.get('rw_rule','never'):<10} {rule.get('super_user','none')}"
        )
    lines.append("")
    return "\n".join(lines)

def cmd_qtree_show(state, _args):
    lines = [
        "",
        "                      Security  Oplock    Qtree      Export",
        "Vserver    Volume     Style     Enabled   ID         Policy",
        "---------- ---------- --------- --------- ---------- ---------",
    ]
    for q in state.get("qtrees", []):
        lines.append(
            f"{q['vserver']:<10} {q['volume']:<10} {q['security_style']:<9} "
            f"{q['oplocks']:<9} {q['qtree_id']:<10} {q['export_policy']}"
        )
    lines.append("")
    return "\n".join(lines)

def cmd_df(state, args):
    volumes = state["volumes"]
    lines = [
        "",
        "Filesystem             Size       Used      Avail Use% Mounted on",
    ]
    for v in volumes:
        lines.append(
            f"/vol/{v['name']:<19} {v['size']:<10} {'N/A':<10} {'N/A':<10} {'N/A'} {v['junction_path']}"
        )
    lines.append("")
    return "\n".join(lines)

# ── config commands ──────────────────────────────────────────────────────────

def _find_volume(state, name, vserver=None):
    for v in state["volumes"]:
        if v["name"] == name:
            if vserver is None or v["vserver"] == vserver:
                return v
    return None

def _find_svm(state, name):
    for s in state["svms"]:
        if s["name"] == name:
            return s
    return None

def _find_lif(state, name, vserver=None):
    for l in state["lifs"]:
        if l["name"] == name:
            if vserver is None or l["vserver"] == vserver:
                return l
    return None

def cmd_volume_create(state, args):
    flags = _parse_flags(args)
    required = ["volume", "vserver", "aggregate", "size"]
    for r in required:
        if r not in flags:
            return f"Error: missing -{r}"
    if _find_volume(state, flags["volume"]):
        return f"Error: Volume '{flags['volume']}' already exists."
    new_vol = {
        "name": flags["volume"],
        "vserver": flags["vserver"],
        "aggregate": flags["aggregate"],
        "size": flags["size"],
        "state": "online",
        "type": "rw",
        "security_style": flags.get("security-style", "unix"),
        "junction_path": flags.get("junction-path", ""),
        "export_policy": flags.get("policy", "default"),
    }
    state["volumes"].append(new_vol)
    save_state(state)
    return f"\n[Job 123] Job succeeded: Create volume succeeded\n"

def cmd_volume_modify(state, args):
    flags = _parse_flags(args)
    vserver = flags.get("vserver")
    volname = flags.get("volume")
    if not volname:
        return "Error: -volume required"
    vol = _find_volume(state, volname, vserver)
    if not vol:
        return f"Error: Volume '{volname}' not found."
    if "policy" in flags:
        svm = _find_svm(state, vol["vserver"])
        if svm and flags["policy"] not in svm["export_policies"]:
            known = ", ".join(svm["export_policies"].keys())
            return (
                f"\nError: export policy '{flags['policy']}' not found for vserver "
                f"'{vol['vserver']}'.\nAvailable policies: {known}\n"
            )
    modifiable = {
        "size": "size",
        "state": "state",
        "junction-path": "junction_path",
        "security-style": "security_style",
        "policy": "export_policy",
    }
    for flag, field in modifiable.items():
        if flag in flags:
            vol[field] = flags[flag]
    save_state(state)
    return f"\nVolume modify successful on volume {volname}.\n"

def cmd_volume_size(state, args):
    flags = _parse_flags(args)
    volname = flags.get("volume") or (args[0] if args else None)
    new_size = flags.get("size") or (args[1] if len(args) > 1 else None)
    if not volname or not new_size:
        return "Usage: volume size -vserver <vs> -volume <vol> -new-size <size>"
    vol = _find_volume(state, volname, flags.get("vserver"))
    if not vol:
        return f"Error: Volume '{volname}' not found."
    old_size = vol["size"]
    vol["size"] = new_size
    save_state(state)
    return f"\nvol size: Volume size for volume {volname} changed from {old_size} to {new_size}.\n"

def cmd_volume_offline(state, args):
    flags = _parse_flags(args)
    volname = flags.get("volume") or (args[0] if args else None)
    if not volname:
        return "Error: -volume required"
    vol = _find_volume(state, volname, flags.get("vserver"))
    if not vol:
        return f"Error: Volume '{volname}' not found."
    vol["state"] = "offline"
    save_state(state)
    return f"\nVolume offline operation on volume {volname} completed successfully.\n"

def cmd_volume_online(state, args):
    flags = _parse_flags(args)
    volname = flags.get("volume") or (args[0] if args else None)
    if not volname:
        return "Error: -volume required"
    vol = _find_volume(state, volname, flags.get("vserver"))
    if not vol:
        return f"Error: Volume '{volname}' not found."
    vol["state"] = "online"
    save_state(state)
    return f"\nVolume online operation on volume {volname} completed successfully.\n"

def cmd_volume_delete(state, args):
    flags = _parse_flags(args)
    volname = flags.get("volume") or (args[0] if args else None)
    if not volname:
        return "Error: -volume required"
    vol = _find_volume(state, volname, flags.get("vserver"))
    if not vol:
        return f"Error: Volume '{volname}' not found."
    state["volumes"].remove(vol)
    save_state(state)
    return f"\nVolume {volname} deleted successfully.\n"

def cmd_volume_mount(state, args):
    flags = _parse_flags(args)
    volname = flags.get("volume")
    jpath = flags.get("junction-path")
    if not volname or not jpath:
        return "Usage: volume mount -vserver <vs> -volume <vol> -junction-path <path>"
    vol = _find_volume(state, volname, flags.get("vserver"))
    if not vol:
        return f"Error: Volume '{volname}' not found."
    vol["junction_path"] = jpath
    save_state(state)
    return f"\nVolume '{volname}' mounted at junction path '{jpath}'.\n"

def cmd_volume_unmount(state, args):
    flags = _parse_flags(args)
    volname = flags.get("volume")
    if not volname:
        return "Error: -volume required"
    vol = _find_volume(state, volname, flags.get("vserver"))
    if not vol:
        return f"Error: Volume '{volname}' not found."
    vol["junction_path"] = ""
    save_state(state)
    return f"\nVolume '{volname}' unmounted from namespace.\n"

def cmd_export_policy_rule_create(state, args):
    flags = _parse_flags(args)
    vserver = flags.get("vserver", "vs_parn_interview")
    policy = flags.get("policyname")
    if not policy:
        return "Error: -policyname required"
    svm = _find_svm(state, vserver)
    if not svm:
        return f"Error: Vserver '{vserver}' not found."
    if policy not in svm["export_policies"]:
        svm["export_policies"][policy] = {"rules": []}
    rules = svm["export_policies"][policy]["rules"]
    next_index = max((r["rule_index"] for r in rules), default=0) + 1
    new_rule = {
        "rule_index": int(flags.get("ruleindex", next_index)),
        "clientmatch": flags.get("clientmatch", "0.0.0.0/0"),
        "ro_rule": flags.get("rorule", "any"),
        "rw_rule": flags.get("rwrule", "never"),
        "super_user": flags.get("superuser", "none"),
    }
    rules.append(new_rule)
    rules.sort(key=lambda r: r["rule_index"])
    save_state(state)
    return f"\nExport policy rule created for policy '{policy}'.\n"

def cmd_export_policy_rule_modify(state, args):
    flags = _parse_flags(args)
    vserver = flags.get("vserver", "vs_parn_interview")
    policy = flags.get("policyname")
    index = int(flags.get("ruleindex", 0))
    if not policy or not index:
        return "Error: -policyname and -ruleindex required"
    svm = _find_svm(state, vserver)
    if not svm or policy not in svm["export_policies"]:
        return f"Error: Policy '{policy}' not found."
    rule = next((r for r in svm["export_policies"][policy]["rules"] if r["rule_index"] == index), None)
    if not rule:
        return f"Error: Rule index {index} not found in policy '{policy}'."
    for flag in ["clientmatch", "rorule", "rwrule", "superuser"]:
        if flag in flags:
            field = {"rorule": "ro_rule", "rwrule": "rw_rule", "superuser": "super_user"}.get(flag, flag)
            rule[field] = flags[flag]
    save_state(state)
    return f"\nExport policy rule {index} in policy '{policy}' modified.\n"

def cmd_export_policy_rule_delete(state, args):
    flags = _parse_flags(args)
    vserver = flags.get("vserver", "vs_parn_interview")
    policy = flags.get("policyname")
    index = int(flags.get("ruleindex", 0))
    if not policy or not index:
        return "Error: -policyname and -ruleindex required"
    svm = _find_svm(state, vserver)
    if not svm or policy not in svm["export_policies"]:
        return f"Error: Policy '{policy}' not found."
    rules = svm["export_policies"][policy]["rules"]
    rule = next((r for r in rules if r["rule_index"] == index), None)
    if not rule:
        return f"Error: Rule index {index} not found."
    rules.remove(rule)
    save_state(state)
    return f"\nExport policy rule {index} deleted from policy '{policy}'.\n"

def cmd_network_interface_modify(state, args):
    flags = _parse_flags(args)
    vserver = flags.get("vserver", "vs_parn_interview")
    lif_name = flags.get("lif")
    if not lif_name:
        return "Error: -lif required"
    lif = _find_lif(state, lif_name, vserver)
    if not lif:
        return f"Error: LIF '{lif_name}' not found."
    if "address" in flags:
        lif["address"] = flags["address"]
    if "netmask" in flags:
        lif["netmask"] = flags["netmask"]
    if "status-admin" in flags:
        lif["status_admin"] = flags["status-admin"]
    save_state(state)
    return f"\nLIF '{lif_name}' modified successfully.\n"

def cmd_reset(state, _args):
    reset_state()
    return "\nSimulator state reset to defaults.\n"

# ── flag parser ──────────────────────────────────────────────────────────────

def _parse_flags(args: list) -> dict:
    """Turn ['-vserver', 'foo', '-volume', 'bar'] → {'vserver': 'foo', 'volume': 'bar'}"""
    result = {}
    i = 0
    while i < len(args):
        token = args[i]
        if token.startswith("-"):
            key = token.lstrip("-")
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                result[key] = args[i + 1]
                i += 2
            else:
                result[key] = True
                i += 1
        else:
            i += 1
    return result

# ── per-command help ─────────────────────────────────────────────────────────

COMMAND_HELP = {
    ("version",): dedent("""
      version
        Display the ONTAP software version string.
    """),

    ("system", "node", "show"): dedent("""
      system node show
        Display health, eligibility, and failover state for all cluster nodes.
    """),

    ("aggr", "show"): dedent("""
      aggr show  |  storage aggregate show
        Display all aggregates with size, state, and owning node.
    """),

    ("volume", "show"): dedent("""
      volume show [options]

      Filter flags:
        -vserver    <name>           Filter by SVM
        -volume     <name>           Filter by volume name
        -aggregate  <name>           Filter by aggregate
        -state      <online|offline> Filter by state

      Output flag:
        -fields <f1,f2,...>  Show only these columns.

      Available fields:
        vserver, volume, size, aggregate, state, type,
        security-style, junction-path, policy

      Examples:
        volume show
        volume show -vserver vs_parn_interview
        volume show -vserver vs_parn_interview -volume interview_test
        volume show -fields vserver,volume,size,aggregate,state,junction-path,policy
    """),

    ("volume", "create"): dedent("""
      volume create -vserver <vs> -volume <vol> -aggregate <agg> -size <size> [options]

      Required:
        -vserver    <name>   SVM name
        -volume     <name>   New volume name
        -aggregate  <name>   Target aggregate
        -size       <size>   e.g. 1GB, 500GB, 2TB

      Optional:
        -junction-path  <path>              Namespace mount point
        -security-style <unix|ntfs|mixed>   Default: unix
        -policy         <export-policy>     Export policy name

      Example:
        volume create -vserver vs_parn_interview -volume myvol \\
          -aggregate n1_SAS_900 -size 100GB -junction-path /myvol
    """),

    ("volume", "modify"): dedent("""
      volume modify -vserver <vs> -volume <vol> [options]

      Modifiable fields:
        -size           <size>             e.g. 2GB
        -state          <online|offline>
        -junction-path  <path>             Namespace path
        -security-style <unix|ntfs|mixed>
        -policy         <export-policy>    Export policy name

      Example:
        volume modify -vserver vs_parn_interview -volume interview_test -policy rad_nfs_policy
    """),

    ("volume", "size"): dedent("""
      volume size -vserver <vs> -volume <vol> -new-size <size>

        -vserver   <name>   SVM name
        -volume    <name>   Volume name
        -new-size  <size>   New size, e.g. 2GB, 1TB

      Example:
        volume size -vserver vs_parn_interview -volume interview_test -new-size 2GB
    """),

    ("volume", "offline"): dedent("""
      volume offline [-vserver <vs>] -volume <vol>
        Take a volume offline. NFS exports will stop serving data.

      Example:
        volume offline -vserver vs_parn_interview -volume interview_test
    """),

    ("volume", "online"): dedent("""
      volume online [-vserver <vs>] -volume <vol>
        Bring an offline volume back online.

      Example:
        volume online -vserver vs_parn_interview -volume interview_test
    """),

    ("volume", "delete"): dedent("""
      volume delete [-vserver <vs>] -volume <vol>
        Permanently delete a volume. Volume must be offline first in production.

      Example:
        volume delete -vserver vs_parn_interview -volume myvol
    """),

    ("volume", "mount"): dedent("""
      volume mount -vserver <vs> -volume <vol> -junction-path <path>
        Mount a volume into the SVM namespace at the given junction path.

        -vserver       <name>   SVM name
        -volume        <name>   Volume to mount
        -junction-path <path>   Namespace path, e.g. /interview_test

      Example:
        volume mount -vserver vs_parn_interview -volume interview_test \\
          -junction-path /interview_test
    """),

    ("volume", "unmount"): dedent("""
      volume unmount -vserver <vs> -volume <vol>
        Remove a volume from the SVM namespace (clears junction-path).

      Example:
        volume unmount -vserver vs_parn_interview -volume interview_test
    """),

    ("network", "interface", "show"): dedent("""
      network interface show [options]

      Filter flags:
        -vserver  <name>   Filter by SVM

      Displays: LIF name, admin/oper status, IP/mask, home node, home port.

      Example:
        network interface show -vserver vs_parn_interview
    """),

    ("network", "interface", "modify"): dedent("""
      network interface modify -vserver <vs> -lif <name> [options]

      Modifiable fields:
        -address      <ip>           New IP address
        -netmask      <mask>         Subnet mask
        -status-admin <up|down>      Admin state

      Example:
        network interface modify -vserver vs_parn_interview \\
          -lif vs_parn_interview_data_lif1 -address <rocky-ip>
    """),

    ("vserver", "show"): dedent("""
      vserver show
        List all SVMs with type, state, root volume, and aggregate.
    """),

    ("vserver", "nfs", "show"): dedent("""
      vserver nfs show
        Display NFS protocol settings for all SVMs (v3/v4 enabled, transports).
    """),

    ("vserver", "export-policy", "show"): dedent("""
      vserver export-policy show
        List all export policies defined on each SVM.
    """),

    ("vserver", "export-policy", "rule", "show"): dedent("""
      vserver export-policy rule show [options]

      Filter flags:
        -vserver    <name>   Filter by SVM
        -policyname <name>   Filter by policy name

      Output flag:
        -fields <f1,f2,...>  Show specific columns.

      Available fields:
        vserver, policyname, ruleindex, protocol,
        clientmatch, rorule, rwrule, superuser, anon

      Examples:
        vserver export-policy rule show
        vserver export-policy rule show -vserver vs_parn_interview
        vserver export-policy rule show -vserver vs_parn_interview -policyname rad_nfs_policy
        vserver export-policy rule show -vserver vs_parn_interview \\
          -policyname rad_nfs_policy -fields clientmatch,rorule,rwrule,superuser,anon
    """),

    ("vserver", "export-policy", "rule", "create"): dedent("""
      vserver export-policy rule create -vserver <vs> -policyname <pol> [options]

      Required:
        -vserver    <name>   SVM name
        -policyname <name>   Export policy name

      Optional:
        -clientmatch <cidr>          Client subnet, e.g. <your-subnet>/24
        -rorule      <sys|any|none>  Read-only security flavor  (default: any)
        -rwrule      <sys|any|none|never>  Read-write security flavor (default: never)
        -superuser   <sys|any|none>  Superuser access           (default: none)
        -ruleindex   <n>             Rule priority index

      Example:
        vserver export-policy rule create -vserver vs_parn_interview \\
          -policyname rad_nfs_policy -clientmatch <your-subnet>/24 \\
          -rorule sys -rwrule sys -superuser sys
    """),

    ("vserver", "export-policy", "rule", "modify"): dedent("""
      vserver export-policy rule modify -vserver <vs> -policyname <pol> -ruleindex <n> [options]

      Required:
        -vserver    <name>   SVM name
        -policyname <name>   Policy name
        -ruleindex  <n>      Rule index to modify

      Modifiable:
        -clientmatch <cidr>
        -rorule      <sys|any|none>
        -rwrule      <sys|any|none|never>
        -superuser   <sys|any|none>

      Example:
        vserver export-policy rule modify -vserver vs_parn_interview \\
          -policyname rad_nfs_policy -ruleindex 1 -clientmatch <your-subnet>/24
    """),

    ("vserver", "export-policy", "rule", "delete"): dedent("""
      vserver export-policy rule delete -vserver <vs> -policyname <pol> -ruleindex <n>

      Example:
        vserver export-policy rule delete -vserver vs_parn_interview \\
          -policyname rad_nfs_policy -ruleindex 1
    """),

    ("qtree", "show"): dedent("""
      qtree show
        List all qtrees with security style, oplock setting, and export policy.
    """),

    ("df",): dedent("""
      df -h
        Display volume usage (size, used, available) and junction paths.
    """),
}

# ── command dispatcher ───────────────────────────────────────────────────────

NFS_SYNC_COMMANDS = {
    ("vserver", "export-policy", "rule", "create"),
    ("vserver", "export-policy", "rule", "modify"),
    ("vserver", "export-policy", "rule", "delete"),
    ("volume", "modify"),
    ("volume", "offline"),
    ("volume", "online"),
    ("volume", "mount"),
    ("volume", "unmount"),
}

COMMANDS = {
    ("version",): cmd_version,
    ("system", "node", "show"): cmd_system_node_show,
    ("aggr", "show"): cmd_aggr_show,
    ("storage", "aggregate", "show"): cmd_aggr_show,
    ("volume", "show"): cmd_volume_show,
    ("network", "interface", "show"): cmd_network_interface_show,
    ("vserver", "show"): cmd_vserver_show,
    ("vserver", "nfs", "show"): cmd_vserver_nfs_show,
    ("vserver", "export-policy", "show"): cmd_export_policy_show,
    ("vserver", "export-policy", "rule", "show"): cmd_export_policy_rule_show,
    ("export-policy", "rule", "show"): cmd_export_policy_rule_show,
    ("qtree", "show"): cmd_qtree_show,
    ("df",): cmd_df,
    ("volume", "create"): cmd_volume_create,
    ("volume", "modify"): cmd_volume_modify,
    ("volume", "size"): cmd_volume_size,
    ("volume", "offline"): cmd_volume_offline,
    ("volume", "online"): cmd_volume_online,
    ("volume", "delete"): cmd_volume_delete,
    ("volume", "mount"): cmd_volume_mount,
    ("volume", "unmount"): cmd_volume_unmount,
    ("vserver", "export-policy", "rule", "create"): cmd_export_policy_rule_create,
    ("vserver", "export-policy", "rule", "modify"): cmd_export_policy_rule_modify,
    ("vserver", "export-policy", "rule", "delete"): cmd_export_policy_rule_delete,
    ("network", "interface", "modify"): cmd_network_interface_modify,
    ("reset", "sim"): cmd_reset,
}

def dispatch(line: str, state: dict) -> str:
    tokens = line.strip().split()
    if not tokens:
        return ""

    # record command for grading
    state["session"]["commands_run"].append(line.strip())

    # task / grade / report commands
    if tokens[0] == "task":
        from tasks import get_task
        return get_task(state, tokens[1:])
    if tokens[0] == "grade":
        from tasks import grade_session
        return grade_session(state)
    if tokens[0] == "report":
        from tasks import report_step
        return report_step(state, tokens[1:])
    if tokens[0] == "cleanup":
        from tasks import cleanup_all
        return cleanup_all(state)
    if tokens[0] in ("exit", "quit", "logout"):
        return "__EXIT__"
    if tokens[0] in ("help", "?"):
        return _cmd_help()

    # trailing ? or help → per-command help
    if tokens[-1] in ("?", "help") and len(tokens) > 1:
        cmd_tokens = tokens[:-1]
        for length in range(min(5, len(cmd_tokens)), 0, -1):
            key = tuple(cmd_tokens[:length])
            if key in COMMAND_HELP:
                return COMMAND_HELP[key]
        return _cmd_help()

    # match longest prefix
    for length in range(min(5, len(tokens)), 0, -1):
        key = tuple(tokens[:length])
        if key in COMMANDS:
            remaining = tokens[length:]
            result = COMMANDS[key](state, remaining)
            if key in NFS_SYNC_COMMANDS:
                try:
                    from tasks import sync_nfs_from_state
                    sync_nfs_from_state(state)
                except Exception:
                    pass
            return result

    return f"\nError: Command not found: '{line.strip()}'\nType 'help' to see available commands.\n"

def _cmd_help():
    return dedent("""
    Available commands:
      version
      system node show
      aggr show | storage aggregate show
      volume show [-vserver <vs>] [-volume <vol>] [-fields <f1,f2,...>]
      volume create -vserver <vs> -volume <vol> -aggregate <agg> -size <sz>
      volume modify -vserver <vs> -volume <vol> [-size|-state|-junction-path|-policy <val>]
      volume size -vserver <vs> -volume <vol> -new-size <sz>
      volume offline|online|delete [-vserver <vs>] -volume <vol>
      volume mount -vserver <vs> -volume <vol> -junction-path <path>
      volume unmount -vserver <vs> -volume <vol>
      network interface show [-vserver <vs>]
      network interface modify -vserver <vs> -lif <name> [-address|-netmask <val>]
      vserver show
      vserver nfs show
      vserver export-policy show
      vserver export-policy rule show [-vserver <vs>] [-policyname <pol>]
      vserver export-policy rule create -vserver <vs> -policyname <pol> -clientmatch <cidr> -rorule <r> -rwrule <r>
      vserver export-policy rule modify -vserver <vs> -policyname <pol> -ruleindex <n> [...]
      vserver export-policy rule delete -vserver <vs> -policyname <pol> -ruleindex <n>
      qtree show
      df -h
      task list           - list all available scenarios
      task storage        - start a random broken-scenario task
      task storage <1-10> - start a specific scenario by number
      grade               - grade your session
      report <step>       - record a completed Linux client step
      report list         - show reportable steps (showmount/mount/findmnt/touch/verify)
      cleanup             - reset server, client, and simulator state to clean baseline
      reset sim          - reset state to defaults
      exit / quit        - disconnect
    """)

# ── SSH server ───────────────────────────────────────────────────────────────

BANNER = dedent("""\r
\r
  NetApp Release 9.12.1 — ONTAP CLI Simulator\r
  Type 'help' for available commands.\r
\r
""")

PROMPT = "cluster1::> "

class OntapSession(asyncssh.SSHServerSession):
    def __init__(self):
        self._state = load_state()
        self._buf = ""
        self._chan = None

    def connection_made(self, chan):
        self._chan = chan

    def shell_requested(self):
        return True

    def session_started(self):
        self._chan.write(BANNER)
        self._chan.write(PROMPT)

    def data_received(self, data, datatype):
        for ch in data:
            if ch in ("\r", "\n"):
                self._chan.write("\r\n")
                line = self._buf.strip()
                self._buf = ""
                if line:
                    result = dispatch(line, self._state)
                    if result == "__EXIT__":
                        self._chan.write("Goodbye.\r\n")
                        self._chan.close()
                        return
                    # convert bare \n → \r\n for terminal
                    result = result.replace("\n", "\r\n")
                    self._chan.write(result)
                self._chan.write(PROMPT)
            elif ch == "\x7f":  # backspace
                if self._buf:
                    self._buf = self._buf[:-1]
                    self._chan.write("\b \b")
            elif ch == "\x03":  # ctrl-c
                self._buf = ""
                self._chan.write("^C\r\n")
                self._chan.write(PROMPT)
            else:
                self._buf += ch
                self._chan.write(ch)

    def eof_received(self):
        self._chan.close()


class OntapServer(asyncssh.SSHServer):
    def begin_auth(self, username):
        return username != SSH_USER

    def password_auth_requested(self):
        return True

    def validate_password(self, username, password):
        return username == SSH_USER and password == SSH_PASS

    def session_requested(self):
        return OntapSession()


async def run_server():
    if not HOST_KEY_PATH.exists():
        HOST_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        key = asyncssh.generate_private_key("ssh-rsa")
        key.write_private_key(str(HOST_KEY_PATH))
        print(f"Generated host key at {HOST_KEY_PATH}")

    await asyncssh.create_server(
        OntapServer,
        host=LISTEN_HOST,
        port=LISTEN_PORT,
        server_host_keys=[str(HOST_KEY_PATH)],
        login_timeout=60,
    )
    print(f"NetApp ONTAP simulator listening on {LISTEN_HOST}:{LISTEN_PORT}")
    print(f"  ssh {SSH_USER}@<host> -p {LISTEN_PORT}   (password: {SSH_PASS})")
    await asyncio.get_event_loop().create_future()  # run forever


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NetApp ONTAP SSH Simulator")
    parser.add_argument("--reset", action="store_true", help="Reset state.json to defaults and exit")
    parser.add_argument("--port", type=int, default=LISTEN_PORT, help="SSH listen port")
    parser.add_argument("--host", default=LISTEN_HOST, help="SSH bind address")
    args = parser.parse_args()

    if args.reset:
        reset_state()
        sys.exit(0)

    LISTEN_PORT = args.port
    LISTEN_HOST = args.host

    asyncio.run(run_server())
