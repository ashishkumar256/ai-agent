{{- define "k8s-ai-agent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "k8s-ai-agent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "k8s-ai-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "k8s-ai-agent.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "k8s-ai-agent.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "k8s-ai-agent.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "k8s-ai-agent.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "k8s-ai-agent.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- required "serviceAccount.name is required when serviceAccount.create is false" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "k8s-ai-agent.credentialsSecretName" -}}
{{- if (values .Values.credentials | compact) -}}
{{- printf "%s-credentials" (include "k8s-ai-agent.fullname" .) -}}
{{- else -}}
{{- .Values.existingCredentialsSecret | default "" -}}
{{- end -}}
{{- end -}}
