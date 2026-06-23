{{- define "recall.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "recall.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "recall.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "recall.labels" -}}
app.kubernetes.io/name: {{ include "recall.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}
