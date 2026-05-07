# EKS Pod Doctor MCP Server

Read-only MCP server for diagnosing pod issues in Kubernetes/EKS.

## What this server provides

- `list_namespaces`
- `list_pods(namespace)`
- `pod_events(namespace, pod_name, limit)`
- `pod_logs(namespace, pod_name, container, tail, previous)`
- `node_conditions`
- `pod_diagnose(namespace, pod_name)`

`pod_diagnose` includes rule-based detection for common failures:

1. `ImagePullBackOff`
2. `ErrImagePull`
3. `CrashLoopBackOff`
4. `CreateContainerConfigError`
5. `RunContainerError`
6. `ContainerCreating` stalls
7. `OOMKilled`
8. non-zero container exits
9. scheduling failures (`PodScheduled=False`)
10. readiness failures (`Ready=False`) + warning event extraction

## Local run

Prereqs:

- Python 3.11+
- `kubectl` and AWS CLI configured for EKS
- kubeconfig generated with:

```bash
aws eks update-kubeconfig --region <REGION> --name <CLUSTER_NAME>
```

Install and run:

```bash
pip install -r requirements.txt
python src/server.py
```

## Docker build

```bash
docker build -t eks-pod-doctor:latest .
```

## Push image to ECR

```bash
aws ecr create-repository --repository-name eks-pod-doctor
aws ecr get-login-password --region <REGION> | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com
docker tag eks-pod-doctor:latest <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/eks-pod-doctor:latest
docker push <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/eks-pod-doctor:latest
```

## Deploy in EKS

1. Apply RBAC and base resources:

```bash
kubectl apply -f k8s/rbac.yaml
```

2. (Optional but recommended) Apply IRSA service account annotation:

- Update placeholders in `k8s/serviceaccount-irsa.yaml`
- Apply:

```bash
kubectl apply -f k8s/serviceaccount-irsa.yaml
```

3. Update image in `k8s/deployment.yaml` and deploy:

```bash
kubectl apply -f k8s/deployment.yaml
kubectl -n mcp-system rollout status deploy/eks-pod-doctor
```

## Security notes

- RBAC is read-only (`get/list/watch`) for pods, logs, events, namespaces, and nodes.
- Keep this service namespace-scoped in your MCP client prompts if needed.
- Avoid exposing raw logs to untrusted users; logs may contain sensitive data.

## MCP client integration

Point your MCP-capable client to this server process/container and invoke `pod_diagnose` first for failed pods, then drill into `pod_events` and `pod_logs` for confirmation.
