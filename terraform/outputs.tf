output "cloud_run_service_uri" {
  description = "Deployed Cloud Run endpoint for the ADK API server."
  value       = google_cloud_run_v2_service.commerce_ops_agent.uri
}

output "runtime_service_account_email" {
  description = "Least-privilege service account bound to the ADK runtime."
  value       = google_service_account.agent_runtime_sa.email
}

output "managed_secret_ids" {
  description = "Google Cloud Secret Manager secret resource IDs."
  value       = [for s in google_secret_manager_secret.commerce_secrets : s.id]
}
