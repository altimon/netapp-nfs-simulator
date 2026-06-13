# NetApp ONTAP SSH Simulator + Linux NFS Lab

A lightweight NetApp ONTAP CLI simulator for practicing storage infrastructure interview tasks. SSH into a realistic `cluster1::>` prompt, run ONTAP commands, work through broken-scenario exercises, and verify your work against a real NFS export.

## Lab Architecture

```
┌─────────────────────────────┐        ┌─────────────────────────────┐
│       Rocky Linux VM        │        │        Ubuntu VM            │
│   (NetApp sim + NFS server) │        │      (NFS client)           │
│                             │        │                             │
│  ssh admin@<ip> -p 2223     │◄──────►│  mount -t nfs <ip>:/srv/…  │
│  /srv/netapp/interview_test │        │  /mnt                       │
│  <rocky-ip>                 │        │  <ubuntu-ip>                │
└─────────────────────────────┘        └─────────────────────────────┘
```

- **Rocky Linux** — runs the ONTAP simulator (asyncssh) and a real NFS server (`nfs-utils`)
- **Ubuntu** — acts as the NFS client; mounts and writes to the export

---

## Quick Start

### 1. Deploy on Rocky Linux

```bash
# Clone/copy files to Rocky VM, then:
sudo bash setup_rocky.sh
```

The script auto-detects your subnet, patches the simulator state with the correct IPs, configures NFS, and starts both services.

**Custom options:**
```bash
sudo bash setup_rocky.sh \
  --subnet 10.0.1.0/24 \
  --port   2224 \
  --lif-ip 10.0.1.10
```

### 2. Connect to the Simulator

```bash
ssh admin@<rocky-ip> -p 2223
# password: netapp123
```

### 3. Verify NFS from Ubuntu

```bash
showmount -e <rocky-ip>
sudo mount -t nfs <rocky-ip>:/srv/netapp/interview_test /mnt
findmnt /mnt
touch /mnt/hello_world
ls -l /mnt/hello_world
sudo umount /mnt
```

---

## Simulated Cluster

| Object | Value |
|--------|-------|
| Cluster | `cluster1` |
| ONTAP version | 9.12.1 |
| Nodes | `parn-test-netapp-n1`, `parn-test-netapp-n2` |
| SVM | `vs_parn_interview` |

### Volumes

| Volume | Aggregate | Size | Junction Path |
|--------|-----------|------|---------------|
| interview_test | n1_SAS_900 | 1GB | /interview_test |
| vol_pacs_images | aggr_sas_01 | 4TB | /pacs_images |
| vol_reports | aggr_sas_01 | 500GB | /reports |
| vol_vmware_nfs | aggr_ssd_01 | 2TB | /vmware_nfs |

### Export Policies

| Policy | Clientmatch | Access |
|--------|-------------|--------|
| default | 0.0.0.0/0 | ro |
| rad_nfs_policy | \<your-subnet\>/24 | rw |
| vmware_nfs_policy | 10.10.30.0/24 | rw |

---

## Simulator Commands

### Show Commands

```
cluster1::> version
cluster1::> system node show
cluster1::> aggr show
cluster1::> storage aggregate show
cluster1::> volume show
cluster1::> volume show -vserver vs_parn_interview
cluster1::> volume show -vserver vs_parn_interview -volume interview_test
cluster1::> volume show -fields vserver,volume,size,aggregate,state,type,security-style,junction-path
cluster1::> network interface show
cluster1::> network interface show -vserver vs_parn_interview
cluster1::> vserver show
cluster1::> vserver nfs show
cluster1::> vserver export-policy show
cluster1::> vserver export-policy rule show
cluster1::> export-policy rule show
cluster1::> qtree show
cluster1::> df -h
```

### Config Commands (persisted to state.json)

```
cluster1::> volume create -vserver <vs> -volume <vol> -aggregate <agg> -size <size>
cluster1::> volume modify -vserver <vs> -volume <vol> -size <size>
cluster1::> volume size   -vserver <vs> -volume <vol> -new-size <size>
cluster1::> volume offline -vserver <vs> -volume <vol>
cluster1::> volume online  -vserver <vs> -volume <vol>
cluster1::> volume delete  -vserver <vs> -volume <vol>
cluster1::> volume mount   -vserver <vs> -volume <vol> -junction-path <path>
cluster1::> volume unmount -vserver <vs> -volume <vol>

cluster1::> vserver export-policy rule create -vserver <vs> -policyname <pol> \
              -clientmatch <cidr> -rorule sys -rwrule sys
cluster1::> vserver export-policy rule modify -vserver <vs> -policyname <pol> \
              -ruleindex <n> -clientmatch <cidr>
cluster1::> vserver export-policy rule delete -vserver <vs> -policyname <pol> -ruleindex <n>

cluster1::> network interface modify -vserver <vs> -lif <name> -address <ip>
```

### Lab Commands

```
cluster1::> task storage    # inject a random broken scenario
cluster1::> grade           # score your session
cluster1::> reset sim       # restore state to defaults
cluster1::> help            # list all commands
cluster1::> exit            # disconnect
```

---

## Task Mode

`task storage` injects one of 10 broken scenarios and asks you to investigate and fix it.

| # | Scenario |
|---|----------|
| 1 | NFS export missing client subnet |
| 2 | Export policy is read-only |
| 3 | Volume `interview_test` is offline |
| 4 | Wrong junction path |
| 5 | Linux firewall blocks NFS *(manual step on Rocky)* |
| 6 | NFS service stopped *(manual step on Rocky)* |
| 7 | Client mounts wrong path *(manual step on Ubuntu)* |
| 8 | Client can mount but cannot write (permissions) |
| 9 | `vol_vmware_nfs` exported read-only by mistake |
| 10 | Data LIF on wrong subnet |

After resolving the issue, run `grade` for a checklist score.

---

## Interview Workflow

The full UCXX-style interview workflow this lab replicates:

```bash
# 1. Connect to simulator
ssh admin@<rocky-ip> -p 2223

# 2. Identify the volume
cluster1::> volume show -vserver vs_parn_interview -volume interview_test

# 3. Note the data LIF / NFS IP
cluster1::> network interface show -vserver vs_parn_interview

# 4. Verify export policy allows your client subnet with rw
cluster1::> vserver export-policy rule show

# 5. From Ubuntu client:
showmount -e <rocky-ip>
sudo mount -t nfs <rocky-ip>:/srv/netapp/interview_test /mnt
findmnt /mnt
nfsstat -m
touch /mnt/hello_world
ls -l /mnt/hello_world
sudo umount /mnt
```

---

## Service Management (Rocky Linux)

```bash
systemctl status  netapp-sim
systemctl restart netapp-sim
systemctl stop    netapp-sim
journalctl -u netapp-sim -f        # live logs

systemctl status  nfs-server
exportfs -v                        # active NFS exports
```

---

## Configuration

All site-specific settings live in `/opt/netapp-sim/config.env` on the server (not committed to git).

Copy `config.env.example` to `config.env` before running `setup_rocky.sh` if you need to override defaults:

```bash
cp config.env.example config.env
# edit config.env, then:
sudo bash setup_rocky.sh
```

| Variable | Default | Description |
|----------|---------|-------------|
| `NETAPP_PORT` | `2223` | Simulator SSH port |
| `NETAPP_USER` | `admin` | Simulator SSH username |
| `NETAPP_PASS` | `netapp123` | Simulator SSH password |
| `NFS_SUBNET` | auto-detected `/24` | Subnet allowed to mount NFS |
| `DATA_LIF_IP` | auto-detected host IP | IP shown for data LIF in ONTAP CLI |
| `ADMIN_LIF_IP` | auto-detected host IP | IP shown for admin LIF in ONTAP CLI |

---

## Reset

```bash
# From inside the simulator:
cluster1::> reset sim

# From the Rocky shell:
python3 /opt/netapp-sim/simulator.py --reset
systemctl restart netapp-sim
```

---

## File Layout

```
.
├── simulator.py          # ONTAP CLI engine (asyncssh SSH server)
├── tasks.py              # Broken-scenario task engine + grader
├── state.json            # Default cluster state (aggregates, volumes, LIFs, policies)
├── config.env.example    # Configuration template — copy to config.env and edit
├── netapp-sim.service    # systemd unit file
├── setup_rocky.sh        # One-shot install script for Rocky Linux
├── test_ubuntu_client.sh # NFS client test script for Ubuntu
└── .gitignore
```

**On the Rocky VM after install:**
```
/opt/netapp-sim/
├── simulator.py
├── tasks.py
├── state.json       # live state, modified by config commands
├── config.env       # site credentials + IPs (gitignored)
└── host_key         # SSH host key (generated on first run)

/srv/netapp/
└── interview_test/  # real NFS export directory
```

---

## Testing All Scenarios

`test_scenarios.py` injects each of the 10 scenarios, checks that both the simulator
state and real NFS server reflect the broken state, applies the canonical fix, and
verifies both sides recover. Run it on Rocky as root after any code change:

```bash
sudo python3 /opt/netapp-sim/test_scenarios.py
```

Expected output ends with:
```
═══════════════════════════════════════════════════════
  TOTAL: N passed, 0 failed
═══════════════════════════════════════════════════════
```

**What it covers per scenario:**

| # | Broken state verified | Fix command | Recovery verified |
|---|-----------------------|-------------|-------------------|
| 1 | sim clientmatch + `/etc/exports` | `vserver export-policy rule modify` | sim + exports |
| 2 | sim rw_rule + `/etc/exports` | `vserver export-policy rule modify` | sim + exports |
| 3 | sim state=offline + exports empty | `volume online` | sim + exports |
| 4 | sim junction_path wrong | `volume mount -junction-path` | sim only |
| 5 | firewall nfs blocked | `firewall-cmd` (manual) | firewall |
| 6 | nfs-server stopped | `systemctl start` (manual) | service status |
| 7 | Ubuntu `/mnt` on wrong path | manual umount+remount | client mount |
| 8 | sim super_user=none + root_squash + dir perms 755 | `vserver export-policy rule modify` | sim + exports + perms |
| 9 | sim vmware rw_rule=never | `vserver export-policy rule modify` | sim only |
| 10 | sim data LIF IP wrong | `network interface modify` | sim only |

---

## Requirements

**Rocky Linux (server):**
- Python 3.9+
- `pip3 install asyncssh`
- `nfs-utils` package
- `firewalld` (optional but recommended)

**Ubuntu (client):**
- `nfs-common` package (`sudo apt install nfs-common`)
