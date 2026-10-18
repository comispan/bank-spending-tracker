# Deploying to AWS Lambda + S3 (single user, password protected)

The serverless alternative to the EC2 setup in [../README.md](../README.md): nothing
runs between requests, so it costs nothing while idle and reaches you from any
browser, not just through an SSH tunnel.

```
browser ──HTTPS + session cookie──▶ Lambda Function URL ──▶ handler.py ──▶ the FastAPI app
                                                           │
                                                           └── S3 (private): app.db, statements/*.pdf
```

| File | Does |
|---|---|
| `template.yaml` | The CloudFormation stack: private versioned bucket, Lambda (arm64), Function URL, role, logs. |
| `handler.py` | Lambda entry point: login page and session cookie, syncs `app.db` and PDFs with S3, runs the app. |
| `build.py` | Builds the zip (`zip`) with Linux arm64 wheels, or hashes a password (`hash`). |
| `deploy.ps1` | Builds, uploads the zip, creates or updates the stack, prints the URL. |
| `data.py` | Copies `data/` up to the bucket (`push`) or down into `backups/` (`pull`). |

## What it costs

For one person, effectively **nothing**: well under US$0.10/month.

- **Lambda:** the always-free tier (1M requests, 400,000 GB-seconds a month) covers
  thousands of statement uploads. A page view is ~0.1 GB-s; a statement ~1 GB-s.
- **Function URL:** free. (API Gateway would add cost and a 29-second timeout.)
- **S3:** a few MB of database and PDFs, plus 30 days of old database versions —
  cents a year. Requests are fractions of a cent.
- **CloudWatch Logs:** kept 14 days; the app logs counts, never descriptions.

No NAT gateway, no VPC, no KMS key, no Secrets Manager — each would add a fixed
monthly charge for nothing a single user needs.

## Deploy

Needs the AWS CLI signed in (`aws configure` or `aws sso login`) and Python 3.12.
Both scripts use the AWS profile `eugene_home_aws` and its region. Pass
`-AwsProfile <name>` to `deploy.ps1`, or set `AWS_PROFILE` for `data.py`, to use another.

```powershell
deploy\aws\deploy.ps1 -SetPassword
```

It prompts for a password (12+ characters; a long random one from a password
manager is best — it is the only lock), builds the zip, and prints the URL. Open it,
and sign in on the login page as `me` with that password. The session lasts 30 days.

Later, to ship new code: `deploy\aws\deploy.ps1` (password unchanged). To change the
password, add `-SetPassword` again. Other options: `-Region`, `-StackName`,
`-AuthUser`, `-SetGeminiKey` (tier 3), `-MaxConcurrency 2` (caps what a flood of
requests could ever cost; new accounts with a 10-instance quota can't reserve any).

## Move your existing statements up

```powershell
python deploy\aws\data.py push <DataBucket output>
```

Copies `data/` to the bucket, rewriting the PDF paths in a copy of the database (your
local one is untouched). Or just open the URL and bulk-upload the PDFs again: the
page sends them in batches of a few MB, under Lambda's 6 MB request limit.

## Back up

The bucket keeps every version of `app.db` for 30 days. For a copy of your own:

```powershell
python deploy\aws\data.py pull <DataBucket output>
```

## Things to know

- **Single uploads over ~4 MB fail** (Lambda's 6 MB request cap, after base64).
  Statements are normally well under 1 MB.
- **First request after idle takes a few seconds** (cold start: load the app,
  download the database, run the startup backfills).
- **Two writes at the same instant:** the second is refused with a "not saved,
  try again" page rather than overwriting the first. Unlikely with one user.
- **Sign out** at `/logout`. Changing the password (`-SetPassword`) signs out every
  browser at once, which is the way to cut off a session you think was copied.
- **Why a login page, not the browser's own prompt:** a Function URL renames the
  `WWW-Authenticate` header, so HTTP Basic auth never shows its prompt there.
- **Deleting the stack keeps the bucket** (`DeletionPolicy: Retain`), so your data
  survives. Empty and delete it yourself if you really mean it.
- The landing page still says statements stay on "this machine". Here, that machine
  is your Lambda and your private bucket, in your own AWS account.
