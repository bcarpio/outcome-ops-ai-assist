terraform {
  backend "s3" {
    bucket = "terraform-state-123456789012-us-west-2-dev"
    key    = "outcome-ops-ai-assist.tfstate"
    region = "us-west-2"
  }
}
