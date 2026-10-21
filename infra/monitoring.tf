resource "aws_sns_topic" "alerts" {
  name = "kmlog-alerts"
}

# Needs one click on the AWS confirmation mail.
resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = local.alert_email
}

# Any failed poll (refresh failure -> re-run the device login, API errors, ...).
resource "aws_cloudwatch_metric_alarm" "poller_errors" {
  alarm_name          = "kmlog-poller-errors"
  alarm_description   = "kmlog-poller failed; check the logs (token refresh failed -> unlink and link the car again on the Auto page)."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.fn["poller"].function_name }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "report_errors" {
  alarm_name          = "kmlog-report-errors"
  alarm_description   = "kmlog-report failed: no maandstaat this month."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.report.function_name }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "web_errors" {
  alarm_name          = "kmlog-web-errors"
  alarm_description   = "kmlog-web raised errors (the web UI shows a blank 502)."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.fn["web"].function_name }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "notify_errors" {
  alarm_name          = "kmlog-notify-errors"
  alarm_description   = "kmlog-notify failed: no question mail / timeouts today."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.notify.function_name }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# 24 invocations a day (inside or outside the poll window); fewer than 16 means the schedule or the
# function is broken.
resource "aws_cloudwatch_metric_alarm" "poller_silent" {
  alarm_name          = "kmlog-poller-invocations-low"
  alarm_description   = "kmlog-poller ran fewer than 16 times in 24 hours."
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions          = { FunctionName = aws_lambda_function.fn["poller"].function_name }
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 16
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_budgets_budget" "monthly" {
  name         = "kmlog-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Only kmlog's own costs: every resource carries the default tag project=kmlog. The account runs
  # other workloads (~$100/month), so an account-wide budget would always be exceeded. The tag must
  # be activated once as a cost allocation tag (Billing -> Cost allocation tags) to be counted.
  cost_filter {
    name   = "TagKeyValue"
    values = ["user:project$kmlog"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [local.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [local.alert_email]
  }
}
