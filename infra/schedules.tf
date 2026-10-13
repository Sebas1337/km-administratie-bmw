# Hourly on the hour, poll_start_hour..poll_end_hour Amsterdam time (default 06-23 = 18 polls/day).
# One poll = one CarData call; BMW allows 50 per 24h, the poller stops itself at quota_limit.
locals {
  poll_wraps    = var.poll_end_hour < var.poll_start_hour
  poll_hours    = local.poll_wraps ? "${var.poll_start_hour}-23,0-${var.poll_end_hour}" : "${var.poll_start_hour}-${var.poll_end_hour}"
  polls_per_day = (local.poll_wraps ? 24 - var.poll_start_hour + var.poll_end_hour : var.poll_end_hour - var.poll_start_hour) + 1
}

resource "aws_scheduler_schedule" "poll" {
  name                         = "kmlog-poll"
  schedule_expression          = "cron(0 ${local.poll_hours} * * ? *)"
  schedule_expression_timezone = "Europe/Amsterdam"

  lifecycle {
    precondition {
      # keep a few calls spare for linking the car and (re)creating the telematic container
      condition     = local.polls_per_day <= var.quota_limit - 5
      error_message = "${local.polls_per_day} polls/day leave too little of quota_limit (${var.quota_limit}); shorten the poll window."
    }
  }

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
