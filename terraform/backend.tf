terraform {
  backend "s3" {
    bucket = "terraform-state-136400015737-us-west-2-dev"
    key    = "outcome-ops-ai-assist.tfstate"
    region = "us-west-2"
  }
}
