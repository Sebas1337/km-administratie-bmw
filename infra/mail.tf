# SES starts in sandbox mode: sender and recipient must both be verified. Sending the maandstaat
# from and to the same address needs this address identity (click the AWS verification mail).
resource "aws_sesv2_email_identity" "report" {
  email_identity = var.report_email
}

# Domain identity, so mail is DKIM-signed by the sender's domain. Without that, a domain that
# publishes DMARC p=reject bounces the mail (550 5.7.26). Default is Easy DKIM: publish the three
# CNAME records from output `dkim_dns_records`. If the DNS provider cannot create CNAMEs under
# _domainkey, set var.dkim_private_key_file (Bring Your Own DKIM): one TXT record instead; the
# private key stays in a local, gitignored file (and in the encrypted state). Changing that key
# requires replacing this resource (the deployer may only create/delete it).
resource "aws_sesv2_email_identity" "domain" {
  email_identity = local.mail_domain

  dynamic "dkim_signing_attributes" {
    for_each = local.byodkim ? [1] : []
    content {
      domain_signing_selector    = var.dkim_selector
      domain_signing_private_key = local.dkim_private_key
    }
  }
}

locals {
  mail_domain = split("@", var.report_email)[1]
  byodkim     = var.dkim_private_key_file != null
  # SES wants the base64 DER body of the PEM, without header/footer lines
  dkim_private_key = local.byodkim ? join("", [
    for line in split("\n", trimspace(file(var.dkim_private_key_file))) : line
    if !startswith(line, "-----")
  ]) : null
  dkim_public_key = local.byodkim ? join("", [
    for line in split("\n", trimspace(file(replace(var.dkim_private_key_file, ".private.pem", ".public.pem")))) : line
    if !startswith(line, "-----")
  ]) : null
}
