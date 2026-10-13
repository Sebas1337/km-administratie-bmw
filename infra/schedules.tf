# Hourly 06:00-23:00 Amsterdam time = 18 polls/day (BMW quota: 50 calls/24h).
resource "aws_scheduler_schedule" "poll" {
  name                         = "kmlog-poll"
  schedule_expression          = "cron(0 6-23 * * ? *)"
  schedule_expression_timezone = "Europe/Amsterdam"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.fn["poller"].arn
    role_arn = aws_iam_role.scheduler.arn
    retry_policy {
      maximum_retry_attempts = 0 # a missed poll is cheaper than a wasted quota call
    }
  }
}

resource "aws_scheduler_schedule" "monthly_report" {
  name                         = "kmlog-monthly-report"
  schedule_expression          = "cron(0 6 1 * ? *)"
  schedule_expression_timezone = "Europe/Amsterdam"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.fn["report"].arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({})
    retry_policy {
      maximum_retry_attempts = 2
    }
  }
}

# Daily at 20:00 Amsterdam: open questions + "gemist?" by mail, timeouts after 7 days.
resource "aws_scheduler_schedule" "notify" {
  name                         = "kmlog-notify"
  schedule_expression          = "cron(0 20 * * ? *)"
  schedule_expression_timezone = "Europe/Amsterdam"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.notify.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({})
    retry_policy {
      maximum_retry_attempts = 1
    }
  }
}
