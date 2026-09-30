# Развёртывание в Kubernetes (Helm)

**🌐 Language / Язык:** [English](../../en/admin/kubernetes-deployment.md) · Русский

> Admin-руководство по развёртыванию полноценного сервера Vesmaro (Vesma)
> в любом кластере Kubernetes 1.25+ — ванильный K8s, K3s, kind, k0s — с помощью
> helm-чарта (`deploy/helm/vesmaro/`): Deployment, Service, **Ingress**,
> два PersistentVolumeClaim и секрет с TOTP-ключом.

---

## Обзор

Чарт разворачивает полную однократную установку:

| Ресурс | Назначение |
|--------|-----------|
| Deployment (1 реплика, стратегия `Recreate`) | HTTP API сервер на порту 8787; SQLite — однописатель, реплики не масштабировать |
| Service (`ClusterIP`) | порт `http` → поды |
| Ingress (включён по умолчанию) | `/` → сервис; класс ингресса и TLS задаются значениями |
| ConfigMap | Рендерит `/app/config.yaml` из values (`api.*`, `embedding.*`, `search.*`, `mcp.*`) |
| Secret (опционально) | Мастер-ключ TOTP; не создаётся, если задан `auth.existingSecret` |
| PVC ×2 | `-data` (SQLite + векторный индекс), `-vault` (Obsidian-зеркало в markdown) |

Поверхность здоровья: неаутентифицированный `GET /health` (используется пробами
и `helm test`).

## Предпосылки

- Kubernetes 1.25+ с рабочим StorageClass (в K3s это `local-path`)
- Helm 3.8+
- Ingress-контроллер (в K3s Traefik установлен из коробки)
- Образ контейнера, доступный кластеру — см. [Статус реестра образов](#статус-реестра-образов)

## Быстрый старт

```bash
helm install vesmaro deploy/helm/vesmaro \
  --namespace vesmaro --create-namespace \
  --set auth.totpMasterKey="$(openssl rand -hex 32)" \
  --set ingress.className=nginx \
  --set 'ingress.hosts[0].host=vesma.example.com'
```

K3s (Traefik и local-path — дефолты, ничего дополнительно не нужно):

```bash
helm install vesmaro deploy/helm/vesmaro \
  --namespace vesmaro --create-namespace \
  --set auth.totpMasterKey="$(openssl rand -hex 32)" \
  --set ingress.className=traefik \
  --set 'ingress.hosts[0].host=mnemos.home.lan'
```

Проверка:

```bash
kubectl -n vesmaro rollout status deploy/vesmaro
helm -n vesmaro test vesmaro            # внутрикластерный wget по /health
kubectl -n vesmaro port-forward svc/vesmaro 8787:8787
curl -fsS http://localhost:8787/health  # → {"status":"ok"}
```

## Мастер-ключ TOTP

Любой не-loopback биндинг **требует** auth + TOTP; пустой мастер-ключ
отвергается на старте, поэтому поды падают в crash-loop, пока ключ не задан.
Три способа, по убыванию предпочтительности:

1. **Заранее созданный секрет (продакшен):**

   ```bash
   kubectl -n vesmaro create secret generic vesmaro-totp \
     --from-literal=totp-master-key="$(openssl rand -hex 32)"
   helm install vesmaro deploy/helm/vesmaro -n vesmaro \
     --set auth.existingSecret=vesmaro-totp
   ```

2. **`--set` при установке** (остаётся в истории релиза — приемлемо для
   хоумлаба): `--set auth.totpMasterKey="$(openssl rand -hex 32)"`.

3. **Values-файл** — никогда не коммитьте реальное значение; держите его
   вне git.

Ключ инжектится под **обоими именами** — `MNEMOS_API__TOTP_MASTER_KEY`
(читают образы 4.x) и `VESMARO_API__TOTP_MASTER_KEY` (читают 5.x+) — из
одного ключа секрета. Это dual-prefix контракт ADR-0031: одно значение,
два имени, — чарт работает через границу ребрендинга 4.3.0 → 5.0.0 без правок.

## Ingress и TLS

```yaml
ingress:
  enabled: true
  className: nginx            # или traefik (дефолт K3s), haproxy, ...
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"   # длинные агентские вызовы
    cert-manager.io/cluster-issuer: letsencrypt-prod         # если установлен cert-manager
  hosts:
    - host: vesma.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: vesmaro-tls
      hosts:
        - vesma.example.com
```

Сервер по умолчанию работает с `behind_tls_proxy: true` и доверяет
`X-Forwarded-For` из `api.trustedProxies` (дефолт — приватные диапазоны).
Если поды вашего ingress-контроллера живут в другом CIDR, добавьте его:

```bash
helm upgrade vesmaro deploy/helm/vesmaro -n vesmaro --reuse-values \
  --set 'api.trustedProxies={10.0.0.0/8,172.16.0.0/12,10.42.0.0/16}'
```

## Хранилище

По умолчанию создаются два PVC (`persistence.data.size: 5Gi`,
`persistence.vault.size: 1Gi`, StorageClass кластера по умолчанию). При
необходимости закрепите класс явно:

```bash
--set persistence.data.storageClass=local-path --set persistence.vault.storageClass=local-path
```

Резервные копии: хранилище — один файл SQLite; снимайте снапшот тома `-data`
(процедура согласованности — [runbooks/backup-restore.md](runbooks/backup-restore.md)).

## Статус реестра образов

Опубликованные образы живут в **`ghcr.io/vesmaro/vesmaro`** (org-неймспейс,
перезалит из легаси-user-неймспейса в волне 4.3.0) и **публичные** — обычный
pull работает без всяких креденшелов. `image.pullSecrets` остаётся доступным
для приватных реестров и rate-limit'ов, но для этого образа не нужен.

Релизный конвейер пока таргетит легаси-имя `ghcr.io/korrnals/vesma` до
миграции реестра в 5.0.0 (ADR-0031 / GWS card #331, фаза g); новые релизы
в это время дотягиваются в org-неймспейс вручную.

## Обновления и удаление

```bash
helm upgrade vesmaro deploy/helm/vesmaro -n vesmaro --reuse-values \
  --set image.tag=4.4.0              # тома с данными переживают обновления
helm uninstall vesmaro -n vesmaro    # PVC сохраняются; при необходимости удалите явно
```

Стратегия `Recreate` намеренная: старый под должен отпустить
ReadWriteOnce-том до того, как его примонтирует новый.

## Разбор неполадок

| Симптом | Причина / исправление |
|---------|----------------------|
| Поды в `CreateContainerConfigError` (или `CrashLoopBackOff` с `ValueError` про мастер-ключ) | Не задан TOTP-ключ — см. [Мастер-ключ TOTP](#мастер-ключ-totp) |
| `ImagePullBackOff` | Auth или rate-limit реестра — проверьте ссылку образа; `image.pullSecrets` для приватных сетапов и rate-limit'ов |
| PVC в `Pending` | Нет StorageClass по умолчанию — задайте `persistence.*.storageClass` |
| Ingress отдаёт 404 | Неверный `ingress.className`, либо контроллер смотрит только другие namespace |
| 401 на `/api/*` | Ожидаемо — все эндпоинты, кроме `/health`, требуют TOTP-логин ([security.md](security.md)) |

## Смотрите также

- [runbooks/container-deployment.md](runbooks/container-deployment.md) — пути Docker / docker-compose / Podman
- [security.md](security.md) — модель угроз, модель аутентификации, enrolment TOTP
- [http-api.md](../user/http-api.md) — REST-эндпоинты
- [`deploy/README.md`](../../../deploy/README.md) — все пути развёртывания одним взглядом
