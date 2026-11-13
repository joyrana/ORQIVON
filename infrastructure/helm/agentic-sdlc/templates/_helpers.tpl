{{- define "asw.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "asw.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "asw.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "asw.labels" -}}
app.kubernetes.io/name: {{ include "asw.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "asw.selector" -}}
app.kubernetes.io/name: {{ include "asw.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "asw.apiImage" -}}
{{- printf "%s:%s" .Values.image.api.repository (default .Chart.AppVersion .Values.image.api.tag) -}}
{{- end -}}

{{- define "asw.webImage" -}}
{{- printf "%s:%s" .Values.image.web.repository (default .Chart.AppVersion .Values.image.web.tag) -}}
{{- end -}}

{{/* Environment shared by the API, the worker and the migration job. */}}
{{- define "asw.env" -}}
- name: APP_ENV
  value: {{ .Values.appEnv | quote }}
- name: DEV_SEED_USERS
  value: "false"
- name: PUBLIC_BASE_URL
  value: {{ required "publicBaseUrl is required" .Values.publicBaseUrl | quote }}
- name: WEB_BASE_URL
  value: {{ required "webBaseUrl is required" .Values.webBaseUrl | quote }}
- name: CORS_ORIGINS
  value: {{ printf "[%q]" .Values.webBaseUrl | quote }}
- name: DATA_DIR
  value: /data
- name: HOME
  value: /tmp
{{- if .Values.config.identity }}
- name: IDENTITY_CONFIG_FILE
  value: /etc/agentic-sdlc/config/identity.yaml
{{- end }}
{{- if .Values.config.gitConnections }}
- name: GIT_CONNECTIONS_FILE
  value: /etc/agentic-sdlc/config/git-connections.yaml
{{- end }}
{{- if .Values.config.models }}
- name: MODEL_REGISTRY_PATH
  value: /etc/agentic-sdlc/config/models.yaml
{{- end }}
{{- if .Values.advisories.enabled }}
- name: SECURITY_ADVISORY_DIR
  value: /data/advisories
{{- end }}
{{- range $k, $v := .Values.env }}
- name: {{ $k }}
  value: {{ $v | quote }}
{{- end }}
{{- end -}}

{{- define "asw.volumes" -}}
- name: data
{{- if .Values.persistence.enabled }}
  persistentVolumeClaim:
    claimName: {{ default (printf "%s-data" (include "asw.fullname" .)) .Values.persistence.existingClaim }}
{{- else }}
  emptyDir: {}
{{- end }}
- name: tmp
  emptyDir: {}
- name: workspaces
  emptyDir:
    sizeLimit: {{ .Values.workspaceSizeLimit }}
- name: config
  configMap:
    name: {{ include "asw.fullname" . }}-config
{{- end -}}

{{- define "asw.mounts" -}}
- name: data
  mountPath: /data
- name: tmp
  mountPath: /tmp
- name: workspaces
  mountPath: /var/tmp
- name: config
  mountPath: /etc/agentic-sdlc/config
  readOnly: true
{{- end -}}

{{/* Settings that would deploy but not work: refuse them at install time. */}}
{{- define "asw.validate" -}}
{{- $_ := required "publicBaseUrl is required" .Values.publicBaseUrl -}}
{{- $_ := required "webBaseUrl is required" .Values.webBaseUrl -}}
{{- $replicas := add .Values.api.replicas .Values.worker.replicas -}}
{{- if and .Values.persistence.enabled (eq .Values.persistence.accessMode "ReadWriteOnce") (gt (int $replicas) 2) -}}
{{- fail "persistence.accessMode ReadWriteOnce cannot be shared by several API and worker pods: use ReadWriteMany storage, or run one API and one worker on the same node" -}}
{{- end -}}
{{- if and (not .Values.persistence.enabled) (gt (int $replicas) 2) -}}
{{- fail "persistence.enabled=false gives each pod its own /data: uploads and repository mirrors would not be shared. Enable persistence for more than one API or worker replica" -}}
{{- end -}}
{{- if not (has .Values.worker.sandboxBackend (list "namespace" "docker")) -}}
{{- fail "worker.sandboxBackend must be namespace or docker" -}}
{{- end -}}
{{- if and (eq .Values.appEnv "production") (not (hasPrefix "https://" .Values.publicBaseUrl)) -}}
{{- fail "publicBaseUrl must be https when appEnv is production" -}}
{{- end -}}
{{- end -}}
