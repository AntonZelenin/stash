# The OpenAI API key, read at cold start by the functions that call OpenAI
# (OPENAI_API_KEY_SECRET_ARN). Terraform only creates the secret; its value
# is set out of band, so the key never enters Terraform files or state:
#
#   aws secretsmanager put-secret-value --secret-id <openai_api_key_secret_arn> --secret-string 'sk-...'
resource "aws_secretsmanager_secret" "openai_api_key" {
  name                    = "${local.name_prefix}/openai/api-key"
  description             = "Stash OpenAI API key (plain string)"
  recovery_window_in_days = 7
}

# Cloudflare Turnstile's secret key, which the API verifies registration
# tokens with (TURNSTILE_SECRET_KEY_SECRET_ARN, read on the first
# registration). Like the OpenAI key, only the secret is created here; its
# value is set out of band:
#
#   aws secretsmanager put-secret-value --secret-id <turnstile_secret_key_secret_arn> --secret-string '0x4AAAA...'
#
# Until it is, registration answers 503 (it never skips the check).
resource "aws_secretsmanager_secret" "turnstile_secret_key" {
  name                    = "${local.name_prefix}/turnstile/secret-key"
  description             = "Stash Cloudflare Turnstile secret key (plain string)"
  recovery_window_in_days = 7
}
