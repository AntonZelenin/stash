# Single-AZ PostgreSQL in the primary DB subnet, next to the Lambda subnet.
# The pgvector extension is created by the application's Alembic migrations
# (CREATE EXTENSION IF NOT EXISTS vector), not here.

locals {
  db_username = "stash"
  db_port     = 5432
}

resource "aws_db_subnet_group" "main" {
  name        = local.name_prefix
  description = "Stash RDS; the second subnet only satisfies the two-AZ requirement"
  subnet_ids  = [for s in aws_subnet.db : s.id]

  tags = { Name = local.name_prefix }
}

# Alphanumeric only, so it can go into a DATABASE_URL without escaping.
# Stored in the (encrypted, private) Terraform state as well as in Secrets
# Manager.
resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_db_instance" "main" {
  identifier = local.name_prefix

  engine                     = "postgres"
  engine_version             = var.db_engine_version
  auto_minor_version_upgrade = true
  instance_class             = var.db_instance_class

  db_name  = var.db_name
  username = local.db_username
  password = random_password.db.result
  port     = local.db_port

  storage_type          = "gp3"
  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  storage_encrypted     = true

  multi_az               = false
  availability_zone      = aws_subnet.db["primary"].availability_zone
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false

  backup_retention_period = var.db_backup_retention_days
  copy_tags_to_snapshot   = true

  deletion_protection       = true
  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.name_prefix}-final"

  # Paid extras, all off.
  performance_insights_enabled = false
  monitoring_interval          = 0

  tags = { Name = local.name_prefix }
}

# Only the migration function reads it (iam.tf), and, while
# var.lambda_master_database_secret_access is on, the others.
resource "aws_secretsmanager_secret" "db" {
  name                    = "${local.name_prefix}/rds/master"
  description             = "Stash RDS master credentials and connection details"
  recovery_window_in_days = 7
}

# Everything needed to build the application's DATABASE_URL:
#   postgresql+asyncpg://{username}:{password}@{host}:{port}/{dbname}
resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({
    engine   = "postgres"
    host     = aws_db_instance.main.address
    port     = aws_db_instance.main.port
    dbname   = aws_db_instance.main.db_name
    username = aws_db_instance.main.username
    password = random_password.db.result
  })
}

# The runtime functions' own database logins (backend/api/src/app/db_roles.py):
# the API's, and one shared by every worker. Not the master user: neither
# owns anything or can change the schema or roles, and the workers' can only
# touch what processing does (no users, tokens or uploads). Terraform only
# generates their credentials; the migration Lambda, as the master user,
# creates the roles with these passwords and grants their privileges after
# every migration, before any function connects with them (the
# deployment's migrations step runs before the functions are updated).
#
# Under <prefix>/rds/ like the master's: the GitHub deploy role may write
# the values Terraform generates there (../github_oidc/policies.tf).
locals {
  db_app_roles = {
    api    = "stash_api"
    worker = "stash_worker"
  }
}

resource "random_password" "db_app" {
  for_each = local.db_app_roles

  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "db_app" {
  for_each = local.db_app_roles

  name                    = "${local.name_prefix}/rds/${each.key}"
  description             = "Stash RDS credentials of the ${each.value} role (${each.key == "api" ? "the API" : "every processing worker"})"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "db_app" {
  for_each = local.db_app_roles

  secret_id = aws_secretsmanager_secret.db_app[each.key].id
  secret_string = jsonencode({
    engine   = "postgres"
    host     = aws_db_instance.main.address
    port     = aws_db_instance.main.port
    dbname   = aws_db_instance.main.db_name
    username = each.value
    password = random_password.db_app[each.key].result
  })
}
