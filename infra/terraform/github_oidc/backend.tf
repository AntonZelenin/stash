# Same state bucket as ../live, under its own key (see backend.hcl.example):
#
#   terraform init -backend-config=backend.hcl
#
# Only ever applied manually, with your own credentials: the GitHub roles
# defined here can't read or change this state, so CI can't widen its own
# permissions.
terraform {
  backend "s3" {
    encrypt      = true
    use_lockfile = true
  }
}
