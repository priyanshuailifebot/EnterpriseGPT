# Infrastructure

Deployment artifacts grouped by target.

```
infra/
├── docker/      # Service-specific Dockerfile overrides (Phase 7)
├── k8s/         # Kubernetes manifests (Phase 7+, optional VPC deploy)
└── terraform/   # IaC for cloud deploy (Phase 7+)
```

In Phase 0 these directories exist as placeholders so the monorepo layout
matches the production target. Concrete manifests are added in Phase 7.
