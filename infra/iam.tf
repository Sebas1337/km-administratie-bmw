# Least-privilege roles; each one is also capped by the permissions boundary (var.boundary_policy_name).

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  for_each             = local.all_functions
  name                 = "kmlog-${each.key}"
  assume_role_policy   = data.aws_iam_policy_document.lambda_assume.json
  permissions_boundary = local.boundary_arn
}

data "aws_iam_policy_document" "poller" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda["poller"].arn}:*"]
  }
  statement {
    sid = "Data"
    actions = [
      "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
      "dynamodb:Query", "dynamodb:BatchWriteItem",
    ]
    resources = [aws_dynamodb_table.data.arn]
  }
  statement {
    sid       = "Tokens"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"]
    resources = [aws_secretsmanager_secret.cardata.arn]
  }
}

data "aws_iam_policy_document" "report" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda["report"].arn}:*"]
  }
  statement {
    sid       = "Data"
    actions   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem"] # PutItem: what was mailed
    resources = [aws_dynamodb_table.data.arn]
  }
  statement {
    sid       = "Reports"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.reports.arn}/reports/*"]
  }
  statement {
    sid       = "Mail"
    actions   = ["ses:SendRawEmail"]
    resources = [aws_sesv2_email_identity.report.arn, aws_sesv2_email_identity.domain.arn]
  }
}

# web UI and notify: read/write the table (answers, overrides, login tokens, counters), mail the
# one user; web also reads its session key and links/unlinks the car (CarData token state).
data "aws_iam_policy_document" "web" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda["web"].arn}:*"]
  }
  statement {
    sid = "Data"
    actions = [
      "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
      "dynamodb:Query", "dynamodb:BatchWriteItem",
    ]
    resources = [aws_dynamodb_table.data.arn]
  }
  statement {
    sid       = "SessionKey"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.web.arn]
  }
  statement {
    sid       = "CarDataTokens"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"]
    resources = [aws_secretsmanager_secret.cardata.arn]
  }
  statement {
    sid       = "Mail"
    actions   = ["ses:SendRawEmail"]
    resources = [aws_sesv2_email_identity.report.arn, aws_sesv2_email_identity.domain.arn]
  }
  statement {
    sid     = "ResendReport" # "Opnieuw mailen"; built ARN, a reference would be a cycle
    actions = ["lambda:InvokeFunction"]
    resources = [
      "arn:aws:lambda:${var.region}:${local.account_id}:function:kmlog-report",
    ]
  }
}

data "aws_iam_policy_document" "notify" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda["notify"].arn}:*"]
  }
  statement {
    sid = "Data"
    actions = [
      "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
      "dynamodb:Query", "dynamodb:BatchWriteItem",
    ]
    resources = [aws_dynamodb_table.data.arn]
  }
  statement {
    sid       = "Mail"
    actions   = ["ses:SendRawEmail"]
    resources = [aws_sesv2_email_identity.report.arn, aws_sesv2_email_identity.domain.arn]
  }
}

locals {
  lambda_policies = {
    poller = data.aws_iam_policy_document.poller.json
    report = data.aws_iam_policy_document.report.json
    web    = data.aws_iam_policy_document.web.json
    notify = data.aws_iam_policy_document.notify.json
  }
}

resource "aws_iam_role_policy" "lambda" {
  for_each = local.all_functions
  name     = "kmlog-${each.key}"
  role     = aws_iam_role.lambda[each.key].id
  policy   = local.lambda_policies[each.key]
}

# --- scheduler ---------------------------------------------------------------------------------

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name                 = "kmlog-scheduler"
  assume_role_policy   = data.aws_iam_policy_document.scheduler_assume.json
  permissions_boundary = local.boundary_arn
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    actions = ["lambda:InvokeFunction"]
    resources = concat(
      [for fn in aws_lambda_function.fn : fn.arn],
      [aws_lambda_function.notify.arn, aws_lambda_function.report.arn],
    )
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "kmlog-scheduler"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}
