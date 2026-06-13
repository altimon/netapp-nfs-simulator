# NetApp ONTAP SSH Simulator + Linux NFS Endpoint

## Project Purpose
A lightweight, local NetApp ONTAP CLI simulator for practicing UCXX-style IT Infrastructure interview tasks.
SSH-accessible, Python-based. Paired with a **real Linux NFS server** so NFS mount/write workflows are genuinely practiced, not just simulated.

## Status
🟡 Starting from scratch — build incrementally, minimal working version first.

## Guiding Principles
- Step-by-step delivery, no over-engineering
- Minimal Python dependencies
- JSON-backed state persistence for simulator
- Real NFS export (not mocked) for client-side practice
- Realistic ONTAP CLI feel, not a real storage stack

---

## Lab Environment
- **Host:** MacBook with VMware Fusion
- **VMs:** Ubuntu VM + Rocky Linux VM (SSH already working between them)
- **Role assignment (Claude's recommendation):**
  - **Rocky Linux VM** = NetApp CLI simulator + real NFS server
  - **Ubuntu VM** = Linux NFS client
- **IPs:** Local private lab IPs only

---

## Architecture

### Stack
- **Language:** Python 3
- **SSH server:** Python SSH library (e.g. `asyncssh` or restricted shell)
- **State:** `/opt/netapp-sim/state.json`
- **Service:** systemd unit
- **NFS server:** standard `nfs-utils` on Rocky Linux

### Directory Layout
```
/opt/netapp-sim/
├── simulator.py       # Main ONTAP CLI engine
├── state.json         # Persisted cluster/volume/LIF state
├── tasks.py           # Broken-scenario task engine
└── README.md

/srv/netapp/
└── interview_test/    # Real NFS export directory
```

### Prompt
```
cluster1::>
```

---

## Simulated Cluster Objects

### Cluster & Nodes
```
Cluster: cluster1
Nodes:   parn-test-netapp-n1
         parn-test-netapp-n2
```

### Aggregates
| Name        | Node               |
|-------------|--------------------|
| aggr_ssd_01 | parn-test-netapp-n1 |
| aggr_sas_01 | parn-test-netapp-n1 |
| n1_SAS_900  | parn-test-netapp-n1 |

### SVM
```
vs_parn_interview
```

### Volumes
| Volume           | Aggregate  | Size | State  | Type | Security | Junction Path    |
|------------------|------------|------|--------|------|----------|------------------|
| interview_test   | n1_SAS_900 | 1GB  | online | RW   | unix     | /interview_test  |
| vol_pacs_images  | aggr_sas_01| 4TB  | online | RW   | unix     | /pacs_images     |
| vol_reports      | aggr_sas_01| 500GB| online | RW   | unix     | /reports         |
| vol_vmware_nfs   | aggr_ssd_01| 2TB  | online | RW   | unix     | /vmware_nfs      |

> `interview_test` is the interview-critical volume.

### LIFs
| LIF Name                         | IP/Mask           | Role  |
|----------------------------------|-------------------|-------|
| vs_parn_interview_admin_lif1     | 192.168.52.31/24  | mgmt  |
| vs_parn_interview_data_lif1      | 192.168.52.42/24  | data  |

> Replace with actual Rocky VM IP for lab use. Keep output format identical.

### Export Policies
| Policy Name       | Clientmatch          | Access |
|-------------------|----------------------|--------|
| default           | 0.0.0.0/0            | ro     |
| rad_nfs_policy    | 192.168.52.0/24      | rw     |
| vmware_nfs_policy | 10.10.30.0/24        | rw     |

> `interview_test` uses `rad_nfs_policy` — allows lab subnet with read-write NFS.

---

## Supported CLI Commands

### Show Commands
```
version
system node show
aggr show
storage aggregate show
volume show
volume show -vserver vs_parn_interview
volume show -vserver vs_parn_interview -volume interview_test
volume show -fields vserver,volume,size,aggregate,state,type,security-style,junction-path
network interface show
network interface show -vserver vs_parn_interview
vserver show
vserver nfs show
vserver export-policy show
vserver export-policy rule show
export-policy rule show
qtree show
df -h
```

### Config Commands (persisted to state.json)
```
volume create
volume modify
volume size
volume offline
volume online
volume delete
volume mount
volume unmount
vserver export-policy rule create
vserver export-policy rule modify
vserver export-policy rule delete
network interface modify
```

> Parser doesn't need to be perfect — support interview-practice syntax and return helpful errors.

---

## Real Linux NFS Setup (Rocky Linux — NFS Server)

```bash
# Install
dnf install -y nfs-utils

# Create export directory
mkdir -p /srv/netapp/interview_test

# /etc/exports
/srv/netapp/interview_test  192.168.52.0/24(rw,sync,no_subtree_check,no_root_squash)

# Enable and start
systemctl enable --now nfs-server
exportfs -rav

# Firewall
firewall-cmd --permanent --add-service=nfs
firewall-cmd --permanent --add-service=mountd
firewall-cmd --permanent --add-service=rpc-bind
firewall-cmd --reload
```

## Linux NFS Practice Commands (Ubuntu — NFS Client)

```bash
showmount -e <rocky-vm-ip>
sudo mount -t nfs <rocky-vm-ip>:/interview_test /mnt
findmnt /mnt
nfsstat -m
touch /mnt/hello_world
ls -l /mnt/hello_world
sudo umount /mnt
```

---

## Simulator ↔ NFS Coordination
The simulator state and real NFS are separate but kept consistent:

| Simulator state              | Real NFS behavior                        |
|------------------------------|------------------------------------------|
| `interview_test` online      | Export exists and is mountable           |
| export policy allows subnet  | `/etc/exports` allows same subnet        |
| export policy broken (task)  | Reproduce by editing `/etc/exports`      |
| volume offline (task)        | Simulate by unexporting the directory    |

First version: consistent simulated output + working real NFS export is sufficient. Deep integration is optional.

---

## Task Mode

### Start a scenario
```
task storage
```
Returns one randomized broken scenario. Do not reveal the fix.

### Scenario pool
1. NFS export missing client subnet
2. Export policy is read-only
3. Volume `interview_test` is offline
4. Wrong junction path shown
5. Linux firewall blocks NFS
6. NFS service stopped on server
7. Client mounts wrong path
8. Client can mount but cannot write (permissions)
9. `vol_vmware_nfs` exported read-only by mistake
10. Data LIF shown on wrong subnet

### Grade the session
```
grade
```
Check and report whether the user:
- [ ] Inspected the volume (`volume show`)
- [ ] Found volume size
- [ ] Found aggregate name
- [ ] Checked SVM LIF/IP (`network interface show`)
- [ ] Checked export policy (`vserver export-policy rule show`)
- [ ] Verified clientmatch/subnet
- [ ] Ran `showmount -e` on Linux client
- [ ] Mounted NFS on `/mnt`
- [ ] Verified with `findmnt` or `nfsstat -m`
- [ ] Created `hello_world`
- [ ] Verified read/write

---

## Build Order (incremental)
1. SSH login → `cluster1::>` prompt appears
2. `volume show` works
3. `aggr show` works
4. `network interface show` works
5. `vserver export-policy rule show` works
6. All remaining `show` commands
7. Config commands + state.json persistence
8. Real NFS export working on Rocky, client mounts from Ubuntu
9. Task mode + grading engine

---

## Deliverables Checklist
- [ ] `simulator.py` — ONTAP CLI engine
- [ ] `state.json` — initial cluster state
- [ ] `tasks.py` — broken scenario engine
- [ ] systemd service file (`netapp-sim.service`)
- [ ] Rocky Linux install + NFS server setup script
- [ ] Ubuntu NFS client test commands
- [ ] `/etc/exports` example
- [ ] `firewall-cmd` rules for Rocky
- [ ] Reset-to-default command (`reset sim`)
- [ ] `README.md` with test + troubleshooting commands

---

## Key Commands for Testing
```bash
# Connect to simulator
ssh admin@<rocky-vm-ip>

# Service management
systemctl status netapp-sim
systemctl restart netapp-sim
journalctl -u netapp-sim -f

# Reset simulator state
python3 /opt/netapp-sim/simulator.py --reset

# NFS server check (Rocky)
exportfs -v
systemctl status nfs-server

# NFS client check (Ubuntu)
showmount -e <rocky-vm-ip>
```

---

## Interview Workflow to Replicate
```
1. ssh admin@<rocky-vm-ip>
2. volume show -vserver vs_parn_interview -volume interview_test
   → note size, aggregate, junction path
3. network interface show -vserver vs_parn_interview
   → note data LIF IP
4. vserver export-policy rule show
   → confirm clientmatch allows lab subnet, access=rw
5. On Ubuntu client:
   showmount -e <rocky-vm-ip>
   sudo mount -t nfs <rocky-vm-ip>:/interview_test /mnt
   findmnt /mnt
   touch /mnt/hello_world
   ls -l /mnt/hello_world
```
