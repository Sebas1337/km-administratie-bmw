variable "region" {
  type    = string
  default = "eu-central-1"
}

variable "report_email" {
  description = "Receives the monthly maandstaat; also the SES sender (verified identity)."
  type        = string
}

variable "alert_email" {
  description = "Receives alarms and budget warnings. Defaults to report_email."
  type        = string
  default     = null
}

variable "vehicle_id" {
  description = "Key for observations/legs; same as `kmlog --vehicle`."
  type        = string
  default     = "car"
}

variable "quota_limit" {
  description = "Max CarData API calls per UTC day the poller may use (BMW limit: 50)."
  type        = number
  default     = 40
}

variable "monthly_budget_usd" {
  type    = number
  default = 5
}

variable "lambda_zip" {
  description = "Built by scripts/build_lambda.sh."
  type        = string
  default     = "../dist/lambda.zip"
}

variable "boundary_policy_name" {
  description = "Customer managed policy used as permissions boundary for every role (infra/iam/)."
  type        = string
  default     = "kmlog-boundary"
}

variable "dkim_selector" {
  description = "BYODKIM only: the TXT record lives at <selector>._domainkey.<mail domain>."
  type        = string
  default     = "kmlog"
}

variable "dkim_private_key_file" {
  description = <<-EOT
    Optional PEM private key for Bring-Your-Own-DKIM (e.g. ../data/dkim/kmlog.private.pem, gitignored),
    for DNS providers that cannot create the CNAME records Easy DKIM needs. Unset = Easy DKIM.
  EOT
  type        = string
  default     = null
}
