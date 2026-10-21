locals {
  lambdas = {
    poller = {
      handler = "kmlog.aws.poller.handler"
      timeout = 60
      memory  = 256
    }
    web = {
      handler = "kmlog.aws.web.handler"
      timeout = 30
      memory  = 512
    }
  }
  # notify and report are their own resources (aws_lambda_function.notify / .report): their
  # environment needs the web function URL (links in the mails), which depends on
  # aws_lambda_function.fn — inside the map that is a cycle.
  notify_fn = {
    handler = "kmlog.aws.notify.handler"
    timeout = 120
    memory  = 512
  }
  report_fn = {
    handler = "kmlog.aws.report.handler"
    timeout = 120
    memory  = 512
  }
  all_functions = merge(local.lambdas, { notify = local.notify_fn, report = local.report_fn })
  web_url       = var.web_domain != null ? "https://${var.web_domain}/" : aws_lambda_function_url.web.function_url
  common_env = {
    TABLE_NAME = aws_dynamodb_table.data.name
    VEHICLE_ID = var.vehicle_id
  }
  lambda_env = {
    poller = merge(local.common_env, {
      SECRET_ID   = aws_secretsmanager_secret.cardata.name
      QUOTA_LIMIT = tostring(var.quota_limit)
    })
    web = merge(local.common_env, {
      REPORT_EMAIL  = var.report_email
      WEB_SECRET_ID = aws_secretsmanager_secret.web.name
      SECRET_ID     = aws_secretsmanager_secret.cardata.name # Auto page: link / unlink the car
      QUOTA_LIMIT   = tostring(var.quota_limit)
      # "Opnieuw mailen" on the month page; a name, not a reference (that would be a cycle)
      REPORT_FUNCTION = "kmlog-report"
      # address/company search on the Instellingen page
      GEOCODER_URL       = var.geocoder_url
      GEOCODER_COUNTRIES = var.geocoder_countries
      },
      var.geocoder_contact == null ? {} : { GEOCODER_CONTACT = var.geocoder_contact },
      # own domain behind CloudFront: the one X-Forwarded-Host the app trusts, and the secret
      # header CloudFront adds so the Function URL cannot be used directly
      var.web_domain == null ? {} : {
        WEB_DOMAIN    = var.web_domain
        ORIGIN_SECRET = random_password.origin_verify[0].result
      },
    )
  }
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each          = local.all_functions
  name              = "/aws/lambda/kmlog-${each.key}"
  retention_in_days = 90
}

resource "aws_lambda_function" "fn" {
  for_each         = local.lambdas
  function_name    = "kmlog-${each.key}"
  role             = aws_iam_role.lambda[each.key].arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = each.value.handler
  timeout          = each.value.timeout
  memory_size      = each.value.memory
  filename         = local.lambda_zip
  source_code_hash = filebase64sha256(local.lambda_zip)
  # cost cap for the public web UI: a flood of requests is throttled instead of scaled out
  reserved_concurrent_executions = each.key == "web" && var.web_max_concurrency != null ? var.web_max_concurrency : -1

  environment {
    variables = local.lambda_env[each.key]
  }

  # the log group above has the default name /aws/lambda/<function>, created before the function
  depends_on = [aws_iam_role_policy.lambda, aws_cloudwatch_log_group.lambda]
}

# Scheduler invokes asynchronously; Lambda's own async retries would spend CarData quota.
resource "aws_lambda_function_event_invoke_config" "no_retry" {
  for_each                     = local.lambdas
  function_name                = aws_lambda_function.fn[each.key].function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 900
}

resource "aws_lambda_function" "notify" {
  function_name    = "kmlog-notify"
  role             = aws_iam_role.lambda["notify"].arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = local.notify_fn.handler
  timeout          = local.notify_fn.timeout
  memory_size      = local.notify_fn.memory
  filename         = local.lambda_zip
  source_code_hash = filebase64sha256(local.lambda_zip)

  environment {
    variables = merge(local.common_env, {
      REPORT_EMAIL = var.report_email
      WEB_URL      = local.web_url
    })
  }

  depends_on = [aws_iam_role_policy.lambda, aws_cloudwatch_log_group.lambda]
}

resource "aws_lambda_function_event_invoke_config" "notify_no_retry" {
  function_name                = aws_lambda_function.notify.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 900
}

resource "aws_lambda_function" "report" {
  function_name    = "kmlog-report"
  role             = aws_iam_role.lambda["report"].arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = local.report_fn.handler
  timeout          = local.report_fn.timeout
  memory_size      = local.report_fn.memory
  filename         = local.lambda_zip
  source_code_hash = filebase64sha256(local.lambda_zip)

  environment {
    variables = merge(local.common_env, {
      REPORTS_BUCKET = aws_s3_bucket.reports.bucket
      REPORT_EMAIL   = var.report_email
      WEB_URL        = local.web_url
    })
  }

  depends_on = [aws_iam_role_policy.lambda, aws_cloudwatch_log_group.lambda]
}

resource "aws_lambda_function_event_invoke_config" "report_no_retry" {
  function_name                = aws_lambda_function.report.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 900
}

# v0.3.0 moved report out of the fn map (it needs the web URL); same function, no replacement
moved {
  from = aws_lambda_function.fn["report"]
  to   = aws_lambda_function.report
}

moved {
  from = aws_lambda_function_event_invoke_config.no_retry["report"]
  to   = aws_lambda_function_event_invoke_config.report_no_retry
}

# --- web UI: public Function URL, authentication in the app (magic link + signed cookie) -------

# Optional own domain (var.web_domain): CloudFront in front of the Function URL. A Function URL only
# answers to its own hostname, so CloudFront cannot forward the viewer's Host header; it sends
# X-Forwarded-Host instead, which the app trusts only when it equals WEB_DOMAIN (kmlog/web/app.py).
# Two-step apply, because CloudFront needs an issued certificate: see infra/README.md "Eigen domein".
resource "aws_acm_certificate" "web" {
  count             = var.web_domain != null ? 1 : 0
  provider          = aws.us_east_1
  domain_name       = var.web_domain
  validation_method = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

# Waits until the DNS record from output `web_domain_dns_records` exists and ACM has issued.
resource "aws_acm_certificate_validation" "web" {
  count           = var.web_domain != null ? 1 : 0
  provider        = aws.us_east_1
  certificate_arn = aws_acm_certificate.web[0].arn
  timeouts {
    create = "30m"
  }
}

# Shared secret CloudFront sends to the web Lambda (X-Origin-Verify); the app rejects requests
# without it, so only the own domain (or the distribution's cloudfront.net name) reaches it.
resource "random_password" "origin_verify" {
  count   = var.web_domain != null ? 1 : 0
  length  = 40
  special = false
}

resource "aws_cloudfront_distribution" "web" {
  count        = var.web_domain != null ? 1 : 0
  enabled      = true
  comment      = "kmlog-web"
  aliases      = [var.web_domain]
  price_class  = "PriceClass_100"
  http_version = "http2and3"

  origin {
    domain_name = trimsuffix(trimprefix(aws_lambda_function_url.web.function_url, "https://"), "/")
    origin_id   = "kmlog-web"
    custom_header {
      name  = "X-Forwarded-Host"
      value = var.web_domain
    }
    custom_header {
      name  = "X-Origin-Verify"
      value = random_password.origin_verify[0].result
    }
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    allowed_methods  = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods   = ["GET", "HEAD"]
    target_origin_id = "kmlog-web"
    compress         = true

    viewer_protocol_policy = "redirect-to-https"
    # AWS managed policies: Managed-CachingDisabled and Managed-AllViewerExceptHostHeader
    # (cookies, query string and headers go to the app, never the viewer's Host)
    cache_policy_id          = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
    origin_request_policy_id = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.web[0].certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

resource "aws_lambda_function_url" "web" {
  function_name      = aws_lambda_function.fn["web"].function_name
  authorization_type = "NONE"
}

# A public (AuthType NONE) Function URL needs both statements since AWS' October 2025 change:
# lambda:InvokeFunctionUrl, and lambda:InvokeFunction restricted to calls via the Function URL.
resource "aws_lambda_permission" "web_url" {
  statement_id           = "FunctionUrlPublic"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.fn["web"].function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "web_invoke_via_url" {
  statement_id             = "FunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.fn["web"].function_name
  principal                = "*"
  invoked_via_function_url = true
}

# Session signing key. Generated by Terraform (and so also in the encrypted state bucket) rather
# than by the Lambda on first use: that would need secretsmanager:CreateSecret, which the
# permissions boundary does not grant. Rotate with `terraform apply -replace=random_password.web_session`
# (logs everybody out).
resource "random_password" "web_session" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "web" {
  name                    = "kmlog/web"
  description             = "HMAC key for web UI session cookies and CSRF tokens"
  recovery_window_in_days = var.allow_destroy ? 0 : 30
}

resource "aws_secretsmanager_secret_version" "web" {
  secret_id     = aws_secretsmanager_secret.web.id
  secret_string = jsonencode({ session_key = random_password.web_session.result })
}
