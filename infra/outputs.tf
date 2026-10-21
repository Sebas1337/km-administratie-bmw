output "table" {
  value = aws_dynamodb_table.data.name
}

output "reports_bucket" {
  value = aws_s3_bucket.reports.bucket
}

output "secret" {
  value = aws_secretsmanager_secret.cardata.name
}

output "functions" {
  value = merge(
    { for k, fn in aws_lambda_function.fn : k => fn.function_name },
    { notify = aws_lambda_function.notify.function_name, report = aws_lambda_function.report.function_name },
  )
}

output "web_url" {
  description = "Web UI (bookmark it on your phone)."
  value       = aws_lambda_function_url.web.function_url
}

output "dkim_dns_records" {
  description = "Publish these records at the DNS provider of the mail domain."
  value = local.byodkim ? [{
    name  = "${var.dkim_selector}._domainkey.${local.mail_domain}"
    type  = "TXT"
    value = "v=DKIM1; k=rsa; p=${local.dkim_public_key}"
    }] : [
    for token in aws_sesv2_email_identity.domain.dkim_signing_attributes[0].tokens : {
      name  = "${token}._domainkey.${local.mail_domain}"
      type  = "CNAME"
      value = "${token}.dkim.amazonses.com"
    }
  ]
}
