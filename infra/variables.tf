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

variable "allow_destroy" {
  description = <<-EOT
    Only to remove the installation (infra/README.md "Verwijderen"): turns off the table's deletion
    protection, lets Terraform empty the reports bucket and deletes secrets without a recovery window.
  EOT
  type        = bool
  default     = false
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

variable "geocoder_url" {
  description = <<-EOT
    Nominatim server for the address/company search on the Instellingen page. The public OSM server
    allows light, user-triggered use (infra/README.md "Adres zoeken"). "off" disables the search.
  EOT
  type        = string
  default     = "https://nominatim.openstreetmap.org"
}

variable "geocoder_countries" {
  description = "Comma-separated ISO country codes the address search is limited to."
  type        = string
  default     = "nl"
}

variable "geocoder_contact" {
  description = "Contact URL in the User-Agent of search requests (OSM usage policy). Unset = this project's repository."
  type        = string
  default     = null
}

variable "web_domain" {
  description = "Custom domain for the web UI (e.g. kmlog.example.com). Needs the ACM + CloudFront rights from infra/iam/deployer-policy.json; see infra/README.md (Eigen domein)."
  type        = string
  default     = null
}

variable "web_max_concurrency" {
  description = "Reserved concurrency of the web Lambda: a cost cap against request floods (one user needs 1-2). null = no cap; the account needs enough unreserved concurrency (AWS keeps at least 10 free)."
  type        = number
  default     = 5
}
