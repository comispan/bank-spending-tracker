# Deploying to AWS (single user, locked to one IP)

One micro EC2 instance. The security group admits **only your static IP, only
on port 22**; the app binds to localhost and is reached through an SSH tunnel.
Nothing is served to the public internet, and nothing travels it unencrypted.

Free on a free-tier-eligible account (750 h/month of a micro instance, 30 GB
EBS, and 750 h/month of public IPv4 for the first year). After that, roughly
US$8–10/month running 24/7 — or nearly nothing if you stop the instance
between uses (step 7).

What lives here:

| File | Does |
|---|---|
| `setup.sh` | First-time server setup: venv, dependencies, systemd unit. Re-run after pulling new code. |
| `spending-tracker.service` | The systemd unit template `setup.sh` installs. |
| `tunnel.ps1` | Run on your PC: opens the SSH tunnel and the browser. |
| `backup.ps1` | Run on your PC: pulls `data/` into a timestamped, gitignored `backups\` folder. |

## 1. Launch the instance (AWS console)

EC2 → Launch instance:

- **AMI:** Ubuntu Server 24.04 LTS, 64-bit x86.
- **Type:** `t3.micro` (`t2.micro` if that's what your region marks free-tier eligible).
- **Key pair:** create one named `spending-tracker`, download the `.pem`, and
  move it to `~\.ssh\spending-tracker.pem` — the helper scripts look there.
- **Network settings → Edit:** create a security group with **one** inbound
  rule: SSH (22), source **Custom**, `<your-static-ip>/32`. Delete any
  0.0.0.0/0 rule the wizard suggests. Do **not** add a rule for 8000.
- **Storage:** the default gp3 volume is fine; confirm **Encrypted** is on.

Everything else can stay default. Note the instance's **public IPv4 address**
once it's running — that's `<ip>` below. Don't allocate an Elastic IP: you
don't need a stable server address (you connect *to* it, nothing connects
back), and an Elastic IP held by a stopped instance bills you.

<details>
<summary>The same thing via AWS CLI</summary>

```bash
MY_IP=<your-static-ip>
SG=$(aws ec2 create-security-group --group-name spending-tracker \
      --description "SSH from home only" --query GroupId --output text)
aws ec2 authorize-security-group-ingress --group-id "$SG" \
      --protocol tcp --port 22 --cidr "$MY_IP/32"
aws ec2 create-key-pair --key-name spending-tracker \
      --query KeyMaterial --output text > ~/.ssh/spending-tracker.pem
AMI=$(aws ssm get-parameter --query Parameter.Value --output text --name \
      /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id)
aws ec2 run-instances --image-id "$AMI" --instance-type t3.micro \
      --key-name spending-tracker --security-group-ids "$SG" \
      --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=spending-tracker}]'
```

</details>

## 2. Get the code onto it

From the repo root on your PC (Git Bash), send exactly what git tracks — no
`.venv`, and no `data/`:

```bash
git archive --format=tar HEAD | ssh -i ~/.ssh/spending-tracker.pem ubuntu@<ip> "mkdir -p bank-spending-tracker && tar -x -C bank-spending-tracker"
```

(If the GitHub repo is reachable from the instance you can `git clone` there
instead; the archive route just avoids putting credentials on the server.)

To carry over the statements and database you already have locally:

```bash
scp -i ~/.ssh/spending-tracker.pem -r data ubuntu@<ip>:bank-spending-tracker/
```

If you use tier 3, put the key in a `.env` at the repo root on the server —
`tier3.py` reads it from there, same as locally:

```bash
ssh -i ~/.ssh/spending-tracker.pem ubuntu@<ip> "echo GEMINI_API_KEY=<key> > bank-spending-tracker/.env"
```

## 3. Set it up

```bash
ssh -i ~/.ssh/spending-tracker.pem ubuntu@<ip> "cd bank-spending-tracker && bash deploy/setup.sh"
```

That installs the venv and dependencies, installs the systemd unit, and starts
the service. It ends by printing the service status — look for `active
(running)`.

## 4. Use it

```powershell
deploy\tunnel.ps1 -InstanceIp <ip>
```

Opens <http://localhost:8000> and holds the tunnel. Keep the window open while
you work; Ctrl+C when done.

## 5. Update the code later

Re-run the `git archive | ssh` line from step 2, then step 3 again (`setup.sh`
is idempotent — it reinstalls dependencies and restarts the service).

## 6. Back up

```powershell
deploy\backup.ps1 -InstanceIp <ip>
```

Copies `data/` to `backups\<timestamp>\` locally. Do this after uploading new
statements — a terminated instance takes the merchant memory and
categorization history with it. `backups/` is gitignored; keep it that way.

## 7. Stop it when idle

EC2 console → instance → **Stop** (not Terminate — Terminate deletes the
disk). Stopped, you pay only for the EBS volume (~US$1–2/month after the free
year). **Start** it again when a statement arrives; the service comes back on
its own (`systemctl enable`), but the public IP will be a **new one** — check
the console and pass the new address to `tunnel.ps1`.

## If your static IP ever changes

The security group is the only door. EC2 console → Security Groups →
`spending-tracker` → Edit inbound rules → update the /32. Until you do,
nothing — including you — can reach the instance.
