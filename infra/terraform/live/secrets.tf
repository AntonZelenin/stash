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
