variable "project_id" {
  description = "Isolated Google Cloud Project ID hosting the ADK agent, Secret Manager, and Cloud DLP."
  type        = string
  default     = "l200-agpalumbo-et3xgz"
}

variable "region" {
  description = "Primary Google Cloud region for Vertex AI and Cloud Run."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Base resource name for Cloud Run and IAM service accounts."
  type        = string
  default     = "alottacake-commerce-ops"
}

variable "container_image" {
  description = "Container image URI built for the ADK API server."
  type        = string
  default     = "us-central1-docker.pkg.dev/l200-agpalumbo-et3xgz/alottacake-commerce-ops-images/agent:latest"
}

variable "commerce_mode" {
  description = "Adapter mode (`live` for Shopify + Cloudflare APIs, `fixture` for synthetic mode)."
  type        = string
  default     = "live"
}

variable "shopify_shop_domain" {
  description = "Target Shopify store domain."
  type        = string
  default     = "m7m2bn-sv.myshopify.com"
}

variable "cloudflare_zone_id" {
  description = "Cloudflare Zone ID for alottacake.com."
  type        = string
  default     = "098b4805a1195d34a58b8320e76872eb"
}
