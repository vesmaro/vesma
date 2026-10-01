{{/* Expand the name of the chart. */}}
{{- define "vesmaro.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Create a default fully qualified app name (63 chars max). */}}
{{- define "vesmaro.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/* Chart name and version as used by the chart label. */}}
{{- define "vesmaro.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end }}

{{/* Common labels. */}}
{{- define "vesmaro.labels" -}}
helm.sh/chart: {{ include "vesmaro.chart" . }}
{{ include "vesmaro.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Selector labels. */}}
{{- define "vesmaro.selectorLabels" -}}
app.kubernetes.io/name: {{ include "vesmaro.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/* Service account name. */}}
{{- define "vesmaro.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "vesmaro.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* TOTP master key secret name. */}}
{{- define "vesmaro.secretName" -}}
{{- default (include "vesmaro.fullname" .) .Values.auth.existingSecret }}
{{- end }}
