# Build the Lambda zip and create or update the CloudFormation stack.
#
#   deploy\aws\deploy.ps1 -SetPassword          # first deploy, or to change the password
#   deploy\aws\deploy.ps1                       # later: new code, same password
#
# Needs the AWS CLI signed in (aws configure / aws sso login) and Python 3.12.
# Runs in Windows PowerShell 5.1 and PowerShell 7 alike.
param(
    [string]$StackName = "spending-tracker",
    [string]$AwsProfile = "eugene_home_aws",
    [string]$Region,
    [switch]$SetPassword,
    [string]$AuthUser,
    [int]$MaxConcurrency = -1,
    [switch]$SetGeminiKey
)
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$awsExe = (Get-Command aws -CommandType Application | Select-Object -First 1).Source

# Native commands are judged by exit code, never by stderr: Windows PowerShell
# 5.1 turns a redirected stderr line into a fatal error under "Stop", and the
# AWS CLI writes progress and expected misses (a 404) there. Every aws call
# goes through these with the one profile, leaving AWS_PROFILE in the calling
# shell alone.
function Invoke-Native([string]$exe, [object[]]$argv) {
    $ErrorActionPreference = "Continue"
    & $exe @argv
    if ($LASTEXITCODE -ne 0) { throw "$(Split-Path $exe -Leaf) $($argv -join ' ') failed (exit $LASTEXITCODE)" }
}
function aws { Invoke-Native $awsExe (@("--profile", $AwsProfile) + $args) }
function py { Invoke-Native "python" $args }

# For calls where failing is an answer, not an error. True on exit code 0;
# the output, if any, is left in $script:probeOut.
function Test-Aws {
    $ErrorActionPreference = "Continue"
    $script:probeOut = & $awsExe --profile $AwsProfile @args 2>$null
    $LASTEXITCODE -eq 0
}

if (-not $Region -and (Test-Aws configure get region)) { $Region = "$probeOut".Trim() }
if (-not $Region) { throw "No region: pass -Region, or set one on profile $AwsProfile." }

$account = aws sts get-caller-identity --query Account --output text
$codeBucket = "$StackName-code-$account-$Region"
Write-Host "Account $account, region $Region, profile $AwsProfile"

# The zip has to be in S3 before the stack can point at it, so its bucket sits
# outside the stack. Its name is guessable, so every call insists the bucket is
# this account's: one someone else created under that name, open to all, would
# otherwise take the zip — and could swap it before Lambda reads it.
if (-not (Test-Aws s3api head-bucket --bucket $codeBucket --expected-bucket-owner $account)) {
    Write-Host "Creating code bucket $codeBucket"
    if ($Region -eq "us-east-1") {
        aws s3api create-bucket --bucket $codeBucket --region $Region | Out-Null
    } else {
        aws s3api create-bucket --bucket $codeBucket --region $Region `
            --create-bucket-configuration "LocationConstraint=$Region" | Out-Null
    }
}
# Private by default today; said outright so a bucket from before that, or one
# loosened by hand, is closed again. Repeating it costs nothing.
aws s3api put-public-access-block --bucket $codeBucket --region $Region `
    --expected-bucket-owner $account --public-access-block-configuration `
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# A cap on simultaneous instances bounds what a flood of requests can cost. It
# needs an account quota above 10 (AWS keeps 10 unreserved), so a new stack
# gets 2 when the account allows it; an existing stack keeps what it has.
$stackExists = Test-Aws cloudformation describe-stacks --stack-name $StackName --region $Region `
    --query "Stacks[0].Parameters[?ParameterKey=='MaxConcurrency'].ParameterValue" --output text
$currentCap = if ($stackExists) { "$probeOut".Trim() } else { "" }
if ($MaxConcurrency -lt 0 -and -not $stackExists) {
    if ((Test-Aws lambda get-account-settings --region $Region `
            --query AccountLimit.ConcurrentExecutions --output text) -and [int]"$probeOut".Trim() -ge 12) {
        $MaxConcurrency = 2
    }
}
if ($MaxConcurrency -eq 0 -or ($MaxConcurrency -lt 0 -and $currentCap -in @("", "0"))) {
    Write-Warning ("Concurrency is uncapped: a flood of requests has no ceiling on cost. " +
        "Pass -MaxConcurrency 2 (needs a Lambda quota above 10), or set an AWS Budget alert.")
}

Write-Host "Building the Lambda zip"
$zip = py "$here\build.py" zip | Select-Object -Last 1
# Named by content, so CloudFormation sees new code as a change and old zips
# stay put for a rollback.
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash.Substring(0, 16).ToLower()
$codeKey = "lambda-$hash.zip"
aws s3api put-object --bucket $codeBucket --key $codeKey --body $zip --region $Region `
    --expected-bucket-owner $account | Out-Null

$overrides = @("CodeBucket=$codeBucket", "CodeKey=$codeKey")
if ($SetPassword) { $overrides += "PasswordHash=$(py "$here\build.py" hash)" }
if ($AuthUser) { $overrides += "AuthUser=$AuthUser" }
if ($MaxConcurrency -ge 0) { $overrides += "MaxConcurrency=$MaxConcurrency" }
if ($SetGeminiKey) {
    $secure = Read-Host "Gemini API key (empty to turn tier 3 off)" -AsSecureString
    $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    $overrides += "GeminiApiKey=$key"
}

# Parameters left out keep their current values on an update.
Write-Host "Deploying stack $StackName"
aws cloudformation deploy --stack-name $StackName --region $Region `
    --template-file "$here\template.yaml" --capabilities CAPABILITY_IAM `
    --parameter-overrides @overrides --no-fail-on-empty-changeset

$url = aws cloudformation describe-stacks --stack-name $StackName --region $Region `
    --query "Stacks[0].Outputs[?OutputKey=='Url'].OutputValue" --output text
Write-Host "`nOpen: $url"
