# PersonalAgent Cloud Deployment Guide (Free Hosting)

This guide documents how to deploy **PersonalAgent** to a suitable free cloud hosting environment so that it operates 24/7 independently of your local laptop.

---

## 1. Hosting Architecture Evaluation

### Application Runtime Model
PersonalAgent combines:
1. **Continuous Telegram Long Polling**: An outbound HTTPS polling loop that listens for owner commands (`run_bot`). It does not require incoming webhooks, open inbound ports, public IPs, or SSL certificates.
2. **Heavy Background Workloads**:
   - `yt-dlp` downloading high-definition YouTube videos (often 500 MB – 2 GB).
   - `faster-whisper` running local neural audio transcription models (requires CPU/RAM).
   - `ffmpeg` performing multi-threaded video transcoding, 9:16 vertical cropping, letterboxing, subtitle burning, and visual filters.
   - OpenCV & MediaPipe running face detection and tracking.
   - `data/` and `review/` state files requiring persistent local disk storage.

### Why "Serverless" Free Tiers Fail
- **Render / Railway / Heroku Free Web Dynos**: Sleep after 15 minutes of HTTP inactivity. They kill background threads, enforce 30–60s request timeouts, and wipe the filesystem on every sleep/restart. They cannot transpile 10-minute videos or maintain persistent polling.
- **Vercel / Cloudflare Workers**: Designed for stateless HTTP micro-functions with strict execution limits (10s to 30s) and no persistent disk or ffmpeg binaries.

### The Recommended Free Platform: Oracle Cloud Always Free Tier (OCI)
Oracle Cloud provides the most generous, genuinely free hosting architecture for long-running AI workers:
- **Compute**: Up to 4 ARM Ampere A1 vCPUs and 24 GB of RAM (can be partitioned into 1–2 VMs), or 2 AMD E2.1.Micro x86 VMs.
- **Storage**: 200 GB of persistent NVMe-backed block volume storage.
- **Network**: Outbound internet connectivity and 10 TB/month free egress.
- **Persistence**: Real, persistent Linux virtual machines (Ubuntu 22.04 LTS or Debian 12) with systemd, Docker, and full background daemon support.
- **Cost**: **$0.00 / month forever** (Always Free tier).

---

## 2. Prerequisites

1. An **Oracle Cloud Infrastructure (OCI)** account (or any persistent Linux VM / VPS).
2. SSH client (`ssh` on Windows PowerShell, macOS, or Linux).
3. Your local configuration files:
   - Configured `.env` file (copied from `.env.example`).
   - `client_secret.json` (Google Cloud OAuth 2.0 Desktop credentials).
   - `token.json` (authenticated YouTube OAuth credentials generated on your laptop).

---

## 3. Provisioning the Free Cloud VM

1. Log in to the [Oracle Cloud Console](https://cloud.oracle.com/).
2. Navigate to **Compute** > **Instances** > **Create Instance**.
3. Configure the VM:
   - **Name**: `personalagent-vm`
   - **Placement**: Default AD.
   - **Image**: **Ubuntu 22.04 LTS Minimal** (or Ubuntu 24.04 / Debian 12).
   - **Shape**:
     - *Recommended*: **Ampere (ARM)** `VM.Standard.A1.Flex` with **2 OCPUs** and **12 GB RAM** (Always Free Eligible).
     - *Alternative*: **AMD** `VM.Standard.E2.1.Micro` with 1 OCPU and 1 GB RAM (Note: RAM is tight for whisper models; swapfile required).
   - **Networking**: Default VCN and public subnet.
   - **SSH Keys**: Download the generated private key or paste your existing public SSH key (`id_rsa.pub` or `id_ed25519.pub`).
   - **Boot Volume**: 50 GB to 100 GB (Always Free allows up to 200 GB total).
4. Click **Create** and wait 60 seconds for the instance status to turn **Running**. Note its public IP address (e.g., `129.153.x.x`).

---

## 4. Initial Server Setup

Connect to your server via SSH:

```bash
ssh -i /path/to/your-ssh-key ubuntu@<YOUR_VM_PUBLIC_IP>
```

Update system packages and install media prerequisites. For a native systemd install, provide Python 3.13.7 from a maintained package source; the Docker image includes the required Python runtime.

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y ffmpeg git curl libgl1 libglib2.0-0
python3.13 --version
```

*(Optional but recommended)* If using an instance with less than 8 GB RAM, create a 4 GB swapfile to ensure smooth video rendering and transcription:

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

---

## 5. Cloning and Setting Up PersonalAgent

Clone your repository onto the server:

```bash
git clone https://github.com/Mohmmadmehdi21/PersonalAgent.git /home/ubuntu/PersonalAgent
cd /home/ubuntu/PersonalAgent
```

Create and activate a Python virtual environment:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Verify that `ffmpeg` and Python can load:

```bash
ffmpeg -version
python -c "import cv2, faster_whisper, numpy; print('Dependencies OK', numpy.__version__)"
```

---

## 6. Secure Secrets & Credentials Configuration

### 1. Configure `.env`
Create the production `.env` file on the server:

```bash
cp .env.example .env
nano .env
```

Ensure the following critical variables are set:
```ini
# Bot Security & Owner Authorization (REQUIRED)
TELEGRAM_BOT_TOKEN="your_bot_token_from_botfather"
TELEGRAM_OWNER_IDS="your_telegram_user_id"
TELEGRAM_CHAT_ID="your_telegram_user_id"

# AI Provider
GROQ_API_KEY="gsk_..."
GROQ_MODEL="llama-3.3-70b-versatile"

# YouTube Channel & Upload Safety
DRY_RUN="false"
GUARD_ENABLED="true"

# Scheduler (Runs inside the bot daemon, no public uploads without owner confirmation)
SCHEDULER_ENABLED="true"
SCHEDULER_TIMEZONE="Asia/Kolkata"
SCHEDULER_PREPARE_ENABLED="true"
SCHEDULER_PREPARE_TIME="09:00"
SCHEDULER_SOURCE_REFRESH_ENABLED="false"
```

> [!CAUTION]
> Never commit `.env`, `token.json`, or `client_secret.json` to GitHub. The `.gitignore` in PersonalAgent already excludes them.

### 2. Copy Google OAuth Credentials
From your local machine (Windows PowerShell), securely transfer your authenticated tokens to the server:

```powershell
scp -i /path/to/your-ssh-key client_secret.json ubuntu@<YOUR_VM_PUBLIC_IP>:/home/ubuntu/PersonalAgent/
scp -i /path/to/your-ssh-key token.json ubuntu@<YOUR_VM_PUBLIC_IP>:/home/ubuntu/PersonalAgent/
# If using growth/analytics tracker:
scp -i /path/to/your-ssh-key token_growth.json ubuntu@<YOUR_VM_PUBLIC_IP>:/home/ubuntu/PersonalAgent/
```

Fix file permissions on the server:

```bash
chmod 600 /home/ubuntu/PersonalAgent/.env
chmod 600 /home/ubuntu/PersonalAgent/*.json
```

---

## 7. Deployment Methods

The local Telegram scheduler and the GitHub Actions workflow are separate. The Telegram scheduler runs only when `SCHEDULER_ENABLED=true` and the bot stays online. The GitHub scheduled workflow runs `main.py prepare` in dry-run mode: it saves drafts and does not publish videos. GitHub Actions caches queue/state/media between runs; cache eviction or quota exhaustion can remove that resumable media, so use persistent VM storage for long-term retention.

### Option A: Native Systemd Service (Recommended)

1. Copy the provided systemd service unit into `/etc/systemd/system/`:

```bash
sudo cp /home/ubuntu/PersonalAgent/personalagent.service /etc/systemd/system/personalagent.service
```

2. Reload systemd and enable the service on boot:

```bash
sudo systemctl daemon-reload
sudo systemctl enable personalagent.service
sudo systemctl start personalagent.service
```

3. Check service status:

```bash
sudo systemctl status personalagent.service
```

4. View real-time logs:

```bash
journalctl -u personalagent.service -f
```

---

### Option B: Docker Compose Deployment

If you prefer containerized management:

1. Install Docker & Docker Compose:

```bash
sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker ubuntu
newgrp docker
```

2. Start the container in detached mode:

```bash
cd /home/ubuntu/PersonalAgent
docker compose up -d --build
```

3. View container logs:

```bash
docker compose logs -f
```

---

## 8. Verification and Health Checks

After starting the service, open Telegram and send commands to your bot:

1. **Test Authorization & Handshake**:
   - Send: `/start`
   - Response: Welcome message and command list.
2. **Verify Integrations & Storage**:
   - Send: `/health`
   - Response should verify:
     - `Telegram: online`
     - `YouTube Data API: configured`
     - `Storage: healthy (data writable, review writable, >10 GB free disk)`
3. **Verify Queue & State**:
   - Send: `/queue`
   - Shows current pending/processing items.
4. **Test Safe Preparation**:
   - Send: `/prepare`
   - Starts a background candidate preparation task (`main.py prepare`).
   - Monitor status with `/task <task_id>`.
5. **Verify Confirmation Workflow**:
   - When clips are ready for upload, `/upload` sends an interactive confirmation card with `✅ Confirm Upload` and `❌ Deny Upload` buttons.
   - Public uploads will **never** proceed without your explicit approval.

---

## 9. Backup and Recovery

- **Queue & Data Backup**:
  The essential files that store your pipeline state are in `data/` and `review/`:
  - `data/used_videos.json`: Tracks sources already processed to prevent duplicate downloads.
  - `data/rejected_ids.json`: Cooldowns for restricted or invalid videos.
  - `data/scheduler.json`: Daily idempotency keys.
  - `review/queue.json`: Clip drafts and metadata.
- **Automated Backup (Cron)**:
  Add a daily backup cron to tar these directories:
  ```bash
  (crontab -l 2>/dev/null; echo "0 3 * * * tar -czf /home/ubuntu/backup_\$(date +\%Y\%m\%d).tar.gz -C /home/ubuntu/PersonalAgent data review") | crontab -
  ```
- **Process Recovery**:
  If the VM or process restarts unexpectedly while a task is running, PersonalAgent's built-in `reconcile_tasks()` detects dead worker PIDs on startup, frees locks, marks orphaned tasks safely, and ensures no clip is left in an unrecoverable corrupted state.
