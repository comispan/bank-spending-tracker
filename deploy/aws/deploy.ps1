# Build the Lambda zip and create or update the CloudFormation stack.
#
#   deploy\aws\deploy.ps1 -SetPassword          # first deploy, or to change the password
#   deploy\aws\deploy.ps1                       # later: new code, same password
#
# Needs the AWS CLI signed in (aws configure / aws sso login) and Python 3.12.
param(
    [string]$StackName = "spending-tracker",
    [string]$Region = $(aws configure get region),
    [switch]$SetPassword,
    [string]$AuthUser,
    [int]$MaxConcurrency = -1,
    [switch]$SetGeminiKey
)
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
if (-not $Region) { throw "No region: pass -Region or run aws configure." }
$here = $PSScriptRoot

$account = aws sts get-caller-identity --query Account --output text
$codeBucket = "$StackName-code-$account-$Region"

# The zip has to be in S3 before the stack can point at it, so its bucket sits
# outside the stack. Private by default; nothing but code goes in it.
$PSNativeCommandUseErrorActionPreference = $false  # a miss is an answer here, not a failure
aws s3api head-bucket --bucket $codeBucket 2>$null
$missing = $LASTEXITCODE -ne 0
$PSNativeCommandUseErrorActionPreference = $true
if ($missing) {
    Write-Host "Creating code bucket $codeBucket"
    if ($Region -eq "us-east-1") {
        aws s3api create-bucket --bucket $codeBucket --region $Region | Out-Null
    } else {
        aws s3api create-bucket --bucket $codeBucket --region $Region `
            --create-bucket-configuration LocationConstraint=$Region | Out-Null
    }
}

Write-Host "Building the Lambda zip"
$zip = python "$here\build.py" zip | Select-Object -Last 1
# Named by content, so CloudFormation sees new code as a change and old zips
# stay put for a rollback.
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash.Substring(0, 16).ToLower()
$codeKey = "lambda-$hash.zip"
aws s3 cp $zip "s3://$codeBucket/$codeKey" --region $Region --only-show-errors

$overrides = @("CodeBucket=$codeBucket", "CodeKey=$codeKey")
if ($SetPassword) { $overrides += "PasswordHash=$(python "$here\build.py" hash)" }
if ($AuthUser) { $overrides += "AuthUser=$AuthUser" }
if ($MaxConcurrency -ge 0) { $overrides += "MaxConcurrency=$MaxConcurrency" }
if ($SetGeminiKey) {
    $key = Read-Host "Gemini API key (empty to turn tier 3 off)" -MaskInput
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
