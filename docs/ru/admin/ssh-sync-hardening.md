# Vesma — чек-лист ужесточения SSH-синхронизации

**🌐 Language / Язык:** [English](../../en/admin/ssh-sync-hardening.md) · Русский

Авто-cron-мост федерации (#104) — ужесточение хост/SSH-слоя для
автоматизации пакетной синхронизации между двумя инстансами vesma
(A = источник, B = цель). Имена вида `mnemos-sync` (пользователь,
каталоги, ключ-комментарии) — легаси-неймс той же установки и
переименования не требуют; env-контракт скрипта — `VESMARO_SYNC_*`
(легаси-имена `MNEMOS_SYNC_*` маппируются с fallback'ом, см.
`scripts/sync-peers.sh`).

## Область, аудитория, связанное

- **Область:** хост/SSH-слой, на котором работают `scripts/sync-peers.sh` и
  юниты `contrib/systemd/mnemos-sync.{service,timer}`. Это НЕ код приложения
  vesma — сам vesma остаётся офлайн.
- **Аудитория:** операторы, разворачивающие пакетную синхронизацию Phase 0
  как автоматизированный cron-мост. Подразумеваются root на обеих машинах
  A и B, обе под Linux с systemd.
- **Связанное:**
  - ArchCom 2026-07-20 — решение об автоматизированном канале (память
    vesma `4dc7d96e`, протокол
    `.archcom/sessions/2026-07-20-automated-channel.md`).
  - Контракт федерации 2026-07-17 §3.1 (память vesma `c64b0c37`,
    `.archcom/sessions/2026-07-17-federation-contract.md`).
  - Оценка Senior Security Engineer — 7 пунктов ужесточения (память
    vesma `ed38f162`).

## Ключевой инвариант

**vesma остаётся офлайн.** У vesma нет входящего эндпоинта — ни
слушающего порта, ни API, открытого в сторону A. Вся автоматизация — на
хост/SSH-слое: A пушит payload поверх rsync+ssh и триггерит импорт поверх
ssh. Украденный SSH-ключ даёт атакующему только `command=""`-ограниченные
операции (rsync-push или триггер импорта), никогда интерактивный shell.

## Пункты ужесточения

### 1. Выделенный пользователь `mnemos-sync` на B

Создайте системного пользователя без shell и с home под `/var/lib`. Этот
пользователь владеет директорией `incoming/` и ограниченным
`authorized_keys`.

```bash
sudo useradd --system --shell /usr/sbin/nologin \
    --home /var/lib/mnemos-sync --create-home mnemos-sync
sudo install -d -o mnemos-sync -g mnemos-sync -m 0750 /var/lib/mnemos-sync/incoming
sudo install -d -o mnemos-sync -g mnemos-sync -m 0700 /var/lib/mnemos-sync/.ssh
```

Директория `incoming/` (`0750`) — куда rsync доставляет payload'ы.
Директория `.ssh/` (`0700`) хранит `authorized_keys`. У пользователя
`mnemos-sync` нет ни пароля, ни shell — вход только по ключу, через два
ограниченных ключа (§2).

### 2. `authorized_keys` на B с ограничениями `command=""`

Два ограниченных ключа, каждый приписан к одной обёртке. Оба несут
allow-лист `from=""`, `no-pty` и все виды forwarding выключенными. Один
ключ сам по себе никогда не даёт shell — выполняется единственная
охраняемая команда.

```text
# ~/.ssh/authorized_keys for mnemos-sync on B

# PUSH key — rsync delivery (rsync-wrapper.sh restricts dest to incoming/)
from="192.0.2.5",no-pty,no-agent-forwarding,no-X11-forwarding,no-port-forwarding,\
command="/usr/local/sbin/rsync-wrapper.sh" \
ssh-ed25519 AAAA... mnemos-sync-push@A

# TRIGGER key — import invocation (vesma-import-wrapper.sh pins passphrase-env)
from="192.0.2.5",no-pty,no-agent-forwarding,no-X11-forwarding,no-port-forwarding,\
command="/usr/local/sbin/vesma-import-wrapper.sh" \
ssh-ed25519 AAAA... mnemos-sync-trigger@A
```

Конкретные реализации:

- `contrib/systemd/rsync-wrapper.sh` — парсит `SSH_ORIGINAL_COMMAND`,
  отклоняет не-rsync вызовы, запирает назначение на `INCOMING_DIR`,
  дописывает строку аудита, затем re-exec `rsync --server`.
- `contrib/systemd/vesma-import-wrapper.sh` — парсит
  `SSH_ORIGINAL_COMMAND`, отклоняет всё, кроме `vesma sync import`,
  переписывает исходный путь под `INCOMING_DIR`, **пиннит
  `--passphrase-env` на сконфигурированное имя** (даже скомпрометированная
  A не может перенаправить чтение парольной фразы), дописывает строку
  аудита, затем exec импорта.

### 3. Генерация ключей Ed25519 на A

Сгенерируйте на A два ключа Ed25519 — один для push, один для триггера.
Два ключа держат две команды на независимых путях отзыва: если push-ключ
скомпрометирован, вы ротируете только его, оставляя триггерный ключ
нетронутым (и наоборот). Один общий ключ при любой компрометации вынудил
бы полную ротацию.

```bash
sudo install -d -o root -g root -m 0750 /etc/vesma
sudo ssh-keygen -t ed25519 -f /etc/vesma/sync-push-key    -N "" -C "mnemos-sync-push@A"
sudo ssh-keygen -t ed25519 -f /etc/vesma/sync-trigger-key -N "" -C "mnemos-sync-trigger@A"
```

| Вариант | Два ключа (выбрано) | Один общий ключ |
| --- | --- | --- |
| Радиус поражения при компрометации | теряет доступ одна команда | теряют доступ обе команды |
| Цена ротации | ротация одного ключа, одна строка `authorized_keys` | ротация одного ключа, две строки |
| Операционная поверхность | два файла ключей к развёртыванию | один файл ключа |

Скопируйте каждый `.pub` на B и добавьте в `authorized_keys` под его
строкой `command=""` (§2). Приватные ключи остаются на A в `/etc/vesma/`
(§4).

### 4. Хранение ключей на A

Приватные ключи лежат в `/etc/vesma/` с `chmod 600`, владелец
`root:root`. Юнит `vesma-sync.service` работает от `mnemos-sync`, но
ключи читаются согласно `User=` юнита systemd — скорректируйте, если ваша
политика требует, чтобы сервисный пользователь владел ключами. Либо
храните ключи в связке ключей ОС (keyring) или в секрет-менеджере (Vault,
systemd-creds) и ссылайтесь на путь в `sync.env`.

```bash
sudo chmod 600 /etc/vesma/sync-push-key /etc/vesma/sync-trigger-key
sudo chown root:root /etc/vesma/sync-push-key /etc/vesma/sync-trigger-key
```

Никогда не коммитьте приватные ключи в VCS. `sync.env.example` ссылается
только на пути — сам ключевой материал передаётся out-of-band.

### 5. Ротация ключей

Ротируйте ежеквартально или немедленно при любом подозрении на
компрометацию.

```text
1. Сгенерируйте новый ключ Ed25519 на A (§3):
     sudo ssh-keygen -t ed25519 -f /etc/vesma/sync-push-key-new -N "" -C "mnemos-sync-push@A-rotN"
2. Добавьте новый .pub в authorized_keys на B (§2) — во время переключения
   оставьте СТАРУЮ строку на месте, чтобы неудавшаяся ротация не сломала cron.
3. Проверьте: запустите sync-peers.sh вручную с VESMA_SYNC_DRY_RUN=1 против
   нового ключа, затем реальный прогон.
4. Обновите sync.env на A, указав путь к новому ключу.
5. Удалите старую строку .pub из authorized_keys на B.
6. Затрите старый приватный ключ на A:  sudo shred -u /etc/vesma/sync-push-key-old
```

### 6. Audit-лог на B

Каждый вызов rsync и импорта дописывает строку в
`/var/log/vesma-sync.log` с ISO-8601 UTC-меткой, IP источника (из
`SSH_CLIENT`), событием (`ACCEPT`/`REJECT`) и деталями. Обе обёртки пишут
через хелпер `_audit` — аудит происходит внутри guard'а `command=""`,
поэтому украденный ключ не может его обойти.

```bash
sudo install -o mnemos-sync -g mnemos-sync -m 0640 /dev/null /var/log/vesma-sync.log
# Optional: logrotate entry for /var/log/vesma-sync.log
```

Формы строк лога (см. `rsync-wrapper.sh` и `vesma-import-wrapper.sh`):

```text
[2026-07-21T12:00:00Z] rsync-wrapper src=192.0.2.5 ACCEPT dest=/var/lib/mnemos-sync/incoming/mnemos-sync-20260721T120000Z.json
[2026-07-21T12:00:05Z] vesma-import-wrapper src=192.0.2.5 ACCEPT source=/var/lib/mnemos-sync/incoming/mnemos-sync-20260721T120000Z.json passphrase-env=VESMA_EXPORT_PASSPHRASE dry_run=0
[2026-07-21T12:01:00Z] rsync-wrapper src=192.0.2.5 REJECT destination outside INCOMING_DIR: /etc/passwd
```

Если агрегируете логи, перенаправьте их на центральный коллектор через
rsyslog:

```text
# /etc/rsyslog.d/mnemos-sync.conf
:syslogtag, contains, "mnemos-sync"  /var/log/vesma-sync.log
& stop
```

### 7. Сеть — allow-лист `from=""` + файрвол

Два слоя ограничивают, кто может достучаться до SSH-поверхности
`mnemos-sync`:

1. **`from=""` в `authorized_keys`** (§2) — ключом может воспользоваться
   только IP A.
2. **Правило файрвола** — до `sshd` для пользователя `mnemos-sync` вообще
   может достучаться только IP A.

```bash
# nftables — allow SSH from A only, drop everything else to port 22
sudo nft add rule inet filter input tcp dport 22 ip saddr 192.0.2.5 accept
sudo nft add rule inet filter input tcp dport 22 drop
```

Пример `sshd_config` — ограничьте пользователя `mnemos-sync` обёртками и
выключите для него все виды forwarding:

```text
# /etc/ssh/sshd_config.d/mnemos-sync.conf
Match User mnemos-sync
    AllowUsers mnemos-sync
    PermitTTY no
    AllowAgentForwarding no
    X11Forwarding no
    AllowTcpForwarding no
    PermitTunnel no
    ForceCommand /usr/local/sbin/vesma-import-wrapper.sh
```

`ForceCommand` — второй эшелон обороны: даже если `command=""` отсутствует
в `authorized_keys`, sshd всё равно вызовет обёртку. Для push-ключа
основным рубежом служит guard `rsync-wrapper.sh` внутри `command=""` —
`ForceCommand` не отличает push от триггера, поэтому обычно он указывает
на скрипт, диспетчеризующий по `$SSH_ORIGINAL_COMMAND`.

## Сводка установки

Шаги по порядку, A → B.

```text
# ── На B (цель) ──────────────────────────────────────────────────────────
1. Создайте пользователя mnemos-sync (§1):
     sudo useradd --system --shell /usr/sbin/nologin --home /var/lib/mnemos-sync --create-home mnemos-sync
2. Создайте incoming/ и .ssh/ с правильными режимами (§1).
3. Установите обёртки:
     sudo install -m 0755 contrib/systemd/rsync-wrapper.sh         /usr/local/sbin/
     sudo install -m 0755 contrib/systemd/vesma-import-wrapper.sh /usr/local/sbin/
4. Создайте /var/log/vesma-sync.log с владельцем mnemos-sync (§6).
5. Добавьте два ограниченных ключа в ~/.ssh/authorized_keys (§2) — после
   того, как публичные ключи A существуют (шаг A1 ниже).
6. Примените sshd_config drop-in + правило файрвола (§7). Перезагрузите sshd.

# ── На A (источник) ──────────────────────────────────────────────────────
1. Сгенерируйте два ключа Ed25519 (§3). chmod 600, владелец root (§4).
2. Скопируйте два файла .pub на B и добавьте их в authorized_keys (шаг B5).
3. Установите scripts/sync-peers.sh:
     sudo install -m 0755 scripts/sync-peers.sh /usr/local/sbin/
4. Разверните /etc/mnemos/sync.env из contrib/systemd/sync.env.example —
   ровно этот путь читает `EnvironmentFile=` юнита `vesma-sync.service`
   (замените каждый RFC-зарезервированный dummy). Парольную фразу
   предоставьте через systemd drop-in или LoadCredential — НЕ в sync.env.
5. Установите systemd-юниты:
     sudo install -m 0644 contrib/systemd/vesma-sync.service /etc/systemd/system/
     sudo install -m 0644 contrib/systemd/vesma-sync.timer   /etc/systemd/system/
     sudo systemctl daemon-reload
8. Сначала dry-run:  sudo VESMA_SYNC_DRY_RUN=1 systemctl start vesma-sync.service
   (или запустите sync-peers.sh руками с экспортированными переменными окружения).
7. Включите таймер:  sudo systemctl enable --now vesma-sync.timer
```

## Проверка

Как убедиться, что ужесточение держится.

| Тест | Ожидаемо | Отказ означает |
| --- | --- | --- |
| `ssh -i sync-push-key mnemos-sync@B` (без команды) | отказ — "no command provided — interactive shell refused." (код 2) | `command=""` не задан в authorized_keys |
| `ssh -i sync-push-key mnemos-sync@B "cat /etc/passwd"` | отказ — "non-rsync command refused" (код 2) | rsync-wrapper.sh не является `command=""` |
| `rsync -e "ssh -i sync-push-key" file B:/etc/passwd` | отказ — "destination outside INCOMING_DIR" (код 2) | сломана проверка пути в rsync-wrapper.sh |
| `ssh -i sync-trigger-key mnemos-sync@B "vesma sync export ..."` | отказ — "non-import command refused" (код 2) | сломан guard vesma-import-wrapper.sh |
| `VESMA_SYNC_DRY_RUN=1 bash scripts/sync-peers.sh` (с env) | код выхода 0, в stderr логируются `vesma sync export`, `rsync`, `ssh` | расхождение env-контракта скрипта |
| `tail /var/log/vesma-sync.log` после реального прогона | строки ACCEPT с src IP + меткой времени | хелпер аудита не пишет |

На каждой новой установке сначала прогоняйте dry-run — он отрабатывает
полную валидацию переменных окружения и построение команд, не трогая сеть.

## См. также

- ArchCom 2026-07-20 — решение об автоматизированном канале (память
  vesma `4dc7d96e`, протокол
  `.archcom/sessions/2026-07-20-automated-channel.md`).
- Контракт федерации 2026-07-17 §3.1 (память vesma `c64b0c37`,
  `.archcom/sessions/2026-07-17-federation-contract.md`).
- Оценка Senior Security Engineer — 7 пунктов ужесточения (память
  vesma `ed38f162`).
- `contrib/systemd/rsync-wrapper.sh` — конкретный guard rsync-push
  (§2, §6).
- `contrib/systemd/vesma-import-wrapper.sh` — конкретный guard триггера
  импорта (§2, §6).
- `contrib/systemd/sync.env.example` — шаблон переменных окружения
  (RFC-зарезервированные dummy).
- `scripts/sync-peers.sh` — скрипт ExecStart (читает `VESMA_SYNC_*`).
- `tests/test_sync_peers_script.py` — тесты скрипта + systemd-юнитов.

---

_Последнее обновление: 2026-09-05_
