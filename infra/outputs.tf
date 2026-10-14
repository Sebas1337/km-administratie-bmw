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
    { notify = aws_lambda_function.notify.function_name },
  )
}

output "web_url" {
  description = "Web UI (bookmark it on your phone)."
  value       = local.web_url
}

output "web_domain_dns_records" {
  description = "Only with web_domain: publish these at the DNS provider (certificate validation first, then the CNAME to CloudFront)."
  value = var.web_domain == null ? [] : concat(
    [for o in aws_acm_certificate.web[0].domain_validation_options : {
      name  = o.resource_record_name
      type  = o.resource_record_type
      value = o.resource_record_value
    }],
    length(aws_cloudfront_distribution.web) == 0 ? [] : [{
      name  = var.web_domain
      type  = "CNAME"
      value = aws_cloudfront_distribution.web[0].domain_name
    }],
  )
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
