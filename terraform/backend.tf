terraform {
  backend "s3" {
    # Backend configuration provided at init time
    # Example usage:
    # terraform init \
    #   -backend-config="bucket=your-state-bucket" \
    #   -backend-config="key=outcomeops/terraform.tfstate" \
    #   -backend-config="region=us-east-1" \
    #   -backend-config="dynamodb_table=your-lock-table"
  }
}
