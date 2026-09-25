# Bootstrap: Terraform state bucket

Creates the S3 bucket that holds the remote state of `../live`, and nothing
else:

- private bucket (ACLs disabled, all public access blocked)
- versioning enabled, so every previous state is recoverable
- SSE-S3 (AES256) encryption by default
- bucket policy that rejects non-TLS requests
- `prevent_destroy`, so a stray `terraform destroy` cannot remove it

Default name: `{project}-tfstate-{account_id}-{region}`, e.g.
`stash-tfstate-123456789012-eu-central-1`. Override with `state_bucket_name`.
One bucket per AWS account; environments in that account share it under
different state keys.

No DynamoDB table: `live` locks with an S3 lockfile (`use_lockfile = true`).

## Workflow

Authenticate first (see [../README.md](../README.md#aws-authentication)).
Optionally copy `terraform.tfvars.example` to `terraform.tfvars` to change the
region or profile; all variables have defaults.

    cd infra/terraform/bootstrap
    terraform init
    terraform plan
    terraform apply

Then read the values for `../live/backend.hcl`:

    terraform output

## Local state

This configuration intentionally keeps its own state in a local, gitignored
`terraform.tfstate`: it cannot store state in a bucket it has not created yet.
The state only describes this one bucket. If it is lost, recover with:

    terraform import aws_s3_bucket.state <bucket-name>

(and likewise for the other `aws_s3_bucket_*` resources), or just keep a copy
of the file somewhere safe.

## How live/ uses the bucket

`../live/backend.tf` declares an `s3` backend with only `encrypt` and
`use_lockfile` set. The bucket, key and region are passed at init time from
`backend.hcl`, built from this configuration's outputs:

    bucket = "<state_bucket_name>"
    key    = "stash/<environment>/terraform.tfstate"
    region = "<state_bucket_region>"

`live` state then lives at `s3://<bucket>/stash/<environment>/terraform.tfstate`,
with a short-lived `terraform.tfstate.tflock` object next to it while an
operation holds the lock.
