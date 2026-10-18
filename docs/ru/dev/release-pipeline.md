# Релизный конвейер (внутренний операторский ранбук)

**🌐 Language / Язык:** [English](../../en/dev/release-pipeline.md) · Русский

> Внутренняя операторская документация: как релизы Vesma идут через общий
> кластерный конвейер. Контрибьюторам и конечным пользователям эта страница
> не нужна — публичные поверхности вклада и релизов описаны в
> [CONTRIBUTING.ru.md](../../../CONTRIBUTING.ru.md) и
> [ранбуке публикации на PyPI](../admin/runbooks/pypi-publish.md).

Проект релизится через общий кластерный конвейер
[Korrnals/release-pipeline](https://github.com/Korrnals/release-pipeline)
(K3s-кластер `abyss-ai-agent`, namespace `release-pipeline`). GitHub Actions
не используется (биллинг аккаунта заблокирован) — конвейер и есть штатный путь
релизов. Релизный артефакт подписывается трёхслойно: SHA256 → SBOM → cosign → GPG.

## Релиз новой версии

1. `VERSION` → релизный коммит (конвенция репо) → тег `vX.Y.Z` → push.
2. Обновить версию проекта в `projects[]` файла
   `~/.cache/release-pipeline-values.yaml` и применить:

   ```bash
   helm upgrade --install release-pipeline \
     ~/LABs/Projects/Project-Umbra/release-pipeline/chart/release-pipeline \
     --kube-context abyss-ai-agent -n release-pipeline \
     -f ~/.cache/release-pipeline-values.yaml
   ```

3. Наблюдение: `kubectl --context abyss-ai-agent -n release-pipeline get jobs`,
   логи: `kubectl ... logs -f job/release-<proj>-<ver>`.

Подробности (типы проектов, kaniko-контейнеры, teardown) — в README самого
конвейера.
