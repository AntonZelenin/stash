# Minimum-cost network: no NAT, no public subnets, no load balancers.
#
#   Lambda (app subnet, dual-stack)
#     → private IPv4 → RDS (db subnets, IPv4 only)
#     → IPv6 → egress-only IGW → internet (OpenAI, AWS dual-stack endpoints)
#
# The egress-only IGW is stateful and outbound-only, so nothing on the
# internet can open a connection into the VPC. No IPv4 internet route exists.

data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  # The app subnet and the primary DB subnet share an AZ, so Lambda → RDS
  # traffic stays in one AZ (no cross-AZ data charges).
  primary_az   = data.aws_availability_zones.available.names[0]
  secondary_az = data.aws_availability_zones.available.names[1]
}

resource "aws_vpc" "main" {
  cidr_block                       = var.vpc_cidr
  assign_generated_ipv6_cidr_block = true
  enable_dns_support               = true
  # Only needed for publicly accessible RDS or interface VPC endpoints with
  # private DNS, neither of which is planned.
  enable_dns_hostnames = var.enable_dns_hostnames

  tags = { Name = local.name_prefix }
}

resource "aws_egress_only_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = { Name = local.name_prefix }
}

# Application (Lambda) subnet: dual-stack, since Lambda does not support
# IPv6-only subnets and reaches RDS over IPv4.

resource "aws_subnet" "app" {
  vpc_id            = aws_vpc.main.id
  availability_zone = local.primary_az
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 0)

  ipv6_cidr_block                 = cidrsubnet(aws_vpc.main.ipv6_cidr_block, 8, 0)
  assign_ipv6_address_on_creation = true

  tags = { Name = "${local.name_prefix}-app-${local.primary_az}" }
}

resource "aws_route_table" "app" {
  vpc_id = aws_vpc.main.id

  route {
    ipv6_cidr_block        = "::/0"
    egress_only_gateway_id = aws_egress_only_internet_gateway.main.id
  }

  tags = { Name = "${local.name_prefix}-app" }
}

resource "aws_route_table_association" "app" {
  subnet_id      = aws_subnet.app.id
  route_table_id = aws_route_table.app.id
}

# Database subnets: the RDS instance is Single-AZ and lives in the primary
# AZ. The secondary subnet exists only because a DB subnet group must span
# at least two AZs.

resource "aws_subnet" "db" {
  for_each = {
    primary   = { az = local.primary_az, netnum = 10 }
    secondary = { az = local.secondary_az, netnum = 11 }
  }

  vpc_id            = aws_vpc.main.id
  availability_zone = each.value.az
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, each.value.netnum)

  tags = { Name = "${local.name_prefix}-db-${each.value.az}" }
}

# No routes beyond the implicit VPC-local ones.
resource "aws_route_table" "db" {
  vpc_id = aws_vpc.main.id

  tags = { Name = "${local.name_prefix}-db" }
}

resource "aws_route_table_association" "db" {
  for_each = aws_subnet.db

  subnet_id      = each.value.id
  route_table_id = aws_route_table.db.id
}

# Strip all rules from the VPC's default security group so nothing can
# accidentally rely on it.
resource "aws_default_security_group" "main" {
  vpc_id = aws_vpc.main.id

  tags = { Name = "${local.name_prefix}-default-unused" }
}

resource "aws_security_group" "app" {
  name        = "${local.name_prefix}-app"
  description = "Stash Lambda functions"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name_prefix}-app" }
}

resource "aws_security_group" "db" {
  name        = "${local.name_prefix}-db"
  description = "Stash RDS PostgreSQL"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name_prefix}-db" }
}

# HTTPS over IPv6: OpenAI and AWS service APIs via their dual-stack endpoints.
resource "aws_vpc_security_group_egress_rule" "app_https_ipv6" {
  security_group_id = aws_security_group.app.id
  description       = "HTTPS to the internet over IPv6"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv6         = "::/0"
}

resource "aws_vpc_security_group_egress_rule" "app_postgres" {
  security_group_id            = aws_security_group.app.id
  description                  = "PostgreSQL to RDS"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.db.id
}

resource "aws_vpc_security_group_ingress_rule" "db_postgres_from_app" {
  security_group_id            = aws_security_group.db.id
  description                  = "PostgreSQL from Stash Lambda functions"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.app.id
}
