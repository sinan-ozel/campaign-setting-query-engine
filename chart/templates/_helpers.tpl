{{/*
Expand the name of the chart.
*/}}
{{- define "csqe.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels applied to every resource.
*/}}
{{- define "csqe.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "csqe.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels for a given component.
Usage: {{ include "csqe.selectorLabels" (dict "root" . "component" "mcp-server") }}
*/}}
{{- define "csqe.selectorLabels" -}}
app.kubernetes.io/name: {{ include "csqe.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Resolve the image tag: .Values.image.tag if set, else .Chart.AppVersion.
*/}}
{{- define "csqe.imageTag" -}}
{{- .Values.image.tag | default .Chart.AppVersion }}
{{- end }}

{{/*
Full image reference for a given component suffix.
Usage: {{ include "csqe.image" (dict "root" . "suffix" "") }}        → org/campaign-setting-query-engine:tag
       {{ include "csqe.image" (dict "root" . "suffix" "-pdf-worker") }}
*/}}
{{- define "csqe.image" -}}
{{- $reg := .root.Values.image.registry -}}
{{- $org := .root.Values.image.organization | required "image.organization is required" -}}
{{- $tag := include "csqe.imageTag" .root -}}
{{- printf "%s/%s/campaign-setting-query-engine%s:%s" $reg $org .suffix $tag }}
{{- end }}

{{/*
Fuseki endpoint — internal service or external override.
*/}}
{{- define "csqe.fusekiEndpoint" -}}
{{- if .Values.fuseki.enabled -}}
http://{{ .Release.Name }}-fuseki:3030/campaign
{{- else -}}
{{ .Values.fuseki.external.endpoint | required "fuseki.external.endpoint is required when fuseki.enabled=false" }}
{{- end }}
{{- end }}

{{/*
Redis URL — internal service or external override.
*/}}
{{- define "csqe.redisUrl" -}}
{{- if .Values.redis.enabled -}}
redis://{{ .Release.Name }}-redis:6379
{{- else -}}
{{ .Values.redis.external.url | required "redis.external.url is required when redis.enabled=false" }}
{{- end }}
{{- end }}

{{/*
MinIO endpoint — internal service or external override.
*/}}
{{- define "csqe.minioEndpoint" -}}
{{- if .Values.minio.enabled -}}
http://{{ .Release.Name }}-minio:9000
{{- else -}}
{{ .Values.minio.external.endpoint | required "minio.external.endpoint is required when minio.enabled=false" }}
{{- end }}
{{- end }}

{{- define "csqe.minioAccessKey" -}}
{{- if .Values.minio.enabled -}}
{{ .Values.minio.rootUser }}
{{- else -}}
{{ .Values.minio.external.accessKey }}
{{- end }}
{{- end }}

{{- define "csqe.minioSecretKey" -}}
{{- if .Values.minio.enabled -}}
{{ .Values.minio.rootPassword }}
{{- else -}}
{{ .Values.minio.external.secretKey }}
{{- end }}
{{- end }}
