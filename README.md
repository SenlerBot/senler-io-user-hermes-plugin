![Senler.io](assets/icon.svg)

# Senler.io User для Hermes Agent

Пакет подключает рабочие инструкции Senler.io к Hermes. Данные и действия
доступны через существующий пользовательский MCP-сервер, с правами вошедшего
пользователя Senler. В пакете два skill: работа с User MCP и обработка входящих.

Версия 0.2.4 — предварительный выпуск для ручной проверки на Windows. В неё входит помощник
настройки: выключенная подписка → проверка выбранных событий → автоматическое
включение через MCP. Сквозной вход и обработку в настоящем Hermes проверяет
владелец установки. Новые возможности требуют соответствующего обновления
Senler API, отправщика вебхуков и User MCP; ZIP не обновляет серверы Senler.

## Установка из GitHub на Windows

Нужен установленный Hermes с поддержкой Agent Plugins v1 и Git. Используйте тот же Hermes и
каталог данных, что и в Desktop; подключение Desktop к другому компьютеру
требует установки на том компьютере.

```powershell
hermes --version
hermes plugins install SenlerBot/senler-io-user-hermes-plugin --no-enable
hermes plugins enable senler-io-user
```

После установки перейдите к разделу **Подключение Senler** ниже: OAuth-вход
выполняется отдельно. Публикация в официальном каталоге Hermes для установки
из этого репозитория не требуется. Если копия из ZIP уже установлена,
сначала выполните переход на GitHub-версию по разделу **Обновление** ниже.

## Установка из ZIP на Windows

Скачайте ZIP из [GitHub Releases](https://github.com/SenlerBot/senler-io-user-hermes-plugin/releases).
Откройте PowerShell в распакованной папке, рядом с `plugin.json`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

`ExecutionPolicy Bypass` действует только для этого процесса. Скрипт проверяет
пакет командой `hermes plugins validate`, копирует файлы и включает плагин
штатной командой Hermes. Существующая копия не перезаписывается.

Каталог данных берётся из `HERMES_HOME`, иначе используется современный
Windows-каталог `%LOCALAPPDATA%\hermes`. Перед записью проверяется наличие
`config.yaml`. Для другой установки или профиля укажите фактический каталог
его `config.yaml`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -HermesHome 'C:\path\to\hermes-home'
```

Для предварительного просмотра добавьте `-WhatIf`. Если команда `hermes` не
найдена, откройте новый PowerShell или передайте фактический путь к CLI через
`-HermesCommand`. Скрипт не устанавливает и не обновляет сам Hermes.

## Подключение Senler

В PowerShell, использующем **тот же каталог данных Hermes**, выполните:

```powershell
hermes mcp add senler-io-user --url 'https://mcp-senler-io-user.senler.io/mcp?auth_mode=oauth' --auth oauth --connect-timeout 300
```

Пройдите вход Senler в открывшемся браузере. Hermes хранит и обновляет OAuth
токены самостоятельно. Если при установке вы передавали `-HermesHome`, сначала
задайте `$env:HERMES_HOME` с тем же путём в этом PowerShell. Desktop тоже должен
использовать этот каталог/профиль.

Проверка:

```powershell
hermes plugins list
hermes mcp test senler-io-user
```

Откройте новую сессию Hermes и отправьте:

> Найди и загрузи skill Senler.io User. Покажи доступные мне проекты Senler:
> названия и ID. Ничего не изменяй.

Полный сценарий проверки — в [docs/windows-test.md](docs/windows-test.md).

В корне намеренно нет `mcp.json`: поддерживаемый Hermes переносимый формат
не содержит настройки OAuth. MCP добавляется отдельной штатной командой;
пакет поставляет skills. Пример конфигурации — [examples/mcp.yaml](examples/mcp.yaml).

## Автоматическая обработка событий

Один раз укажите публичный HTTPS-адрес своего Hermes и разрешите этому плагину
использовать User MCP в фоновом обработчике: [docs/webhooks.md](docs/webhooks.md).
Затем в обычном чате можно попросить:

> Обрабатывай входящие сообщения проекта «Учебный»: классифицируй вопрос и
> готовь черновик ответа. Сообщения клиентам пока не отправляй.

Hermes создаст подписку, проверит приём и включит её в рамках той же просьбы.
Можно выбрать другие события, отдельные каналы, изменить правила или поставить
обработку на паузу. User OAuth остаётся одним на пользователя; каждая подписка
привязана к выбранному проекту.

В новом чате Hermes сначала находит сохранённые подключения. Повторная просьба
использует прежний сценарий и ID вебхука. При остановке сначала выключается
локальный обработчик, затем подписка Senler. Если Senler или OAuth недоступны,
Hermes сообщает, что обработчик остановлен локально, а отключение подписки ещё
не подтверждено. Уже выполняющаяся задача может продолжить работу.

Черновик сохраняется в сессии обработки Hermes. Чтобы увидеть результаты,
попросите: «Покажи последние черновики обработки Senler проекта „Учебный“».
Плагин помогает найти сохранённые ответы в истории сессий; они не появляются
автоматически в исходном чате и не сохраняются как черновики в Senler.
Журнал gateway содержит только короткое начало ответа.

Пакет не запускает listener, не создаёт подписки, не отправляет сообщения при
установке и не добавляет фоновый опрос диалогов.

## Обновление

Копия, установленная из GitHub, обновляется штатной командой:

```powershell
hermes plugins update senler-io-user
```

ZIP-копия не имеет Git-источника для `hermes plugins update`. Перед заменой
сохраните свои правки внутри папки плагина, если они есть. Удалите только этот
плагин штатной командой, затем выполните установку из GitHub выше или установите
новую ZIP-версию:

```powershell
hermes plugins remove senler-io-user
```

MCP-подключение управляется отдельно. Не запускайте `mcp add` заново при каждом
обновлении: сначала проверьте `hermes mcp test senler-io-user`. При истёкшей
авторизации используйте `hermes mcp login senler-io-user`.

После обновления попросите Hermes применить текущие правила к существующей
подписке: помощник повторит проверку и включение того же ID. Это обновит
инструкцию, встроенную в маршрут. Само удаление пакета оставляет MCP и маршруты;
для полного отключения сначала попросите отключить соответствующую интеграцию.

Исходники и выпуски: [SenlerBot/senler-io-user-hermes-plugin](https://github.com/SenlerBot/senler-io-user-hermes-plugin).
Не добавляйте в репозиторий папку данных Hermes, OAuth-токены и `webhook_subscriptions.json`.

## Логотип и формат пакета

В `assets/icon.svg` включён исходный логотип Senler из пользовательского
плагина для Codex. Он отображается в README и копируется при установке.
Манифест соответствует Agent Plugins v1; поля названия, версии, автора,
сайта и лицензии заполнены.

У переносимого `plugin.json` нет поля `icon`. Изображение карточки в каталоге
Hermes задаётся отдельным полем `image` в записи этого каталога. Наличие SVG в
ZIP само по себе не назначает значок плагину в Desktop. Пакет ещё не опубликован
в каталоге. Подробности проверки и публикации — [docs/packaging.md](docs/packaging.md).

## Проверки разработчика

Манифест проверен по официальной JSON Schema Agent Plugins 1.0.0. Загрузчик из
исходников Hermes на указанном в [packaging.md](docs/packaging.md) коммите
распознал оба skill без диагностик. Проверка установщика выполняется в PowerShell с тестовой
командой вместо Hermes, во временном каталоге: проверяются успешная установка,
копирование логотипа, пути с пробелами, `-WhatIf`, сохранение старой копии
и остановка при ошибках.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\install.Tests.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\run-helper.Tests.ps1
python -m unittest discover -s tests -p 'test_*.py'
```

Эти проверки не выполняют вход в Senler и не заменяют тест в настоящем Hermes.

## Источники совместимости

Проверено по документации и исходникам Hermes 10 октября 2026:

- [Portable Agent Plugins](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins#portable-agent-plugins-v1-packages)
- [Native Windows](https://hermes-agent.nousresearch.com/docs/user-guide/windows-native)
- [MCP OAuth](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp/#oauth-authenticated-http-servers)
- [Похожая схема OAuth-установки](https://hermes-agent.nousresearch.com/docs/plugins/letsfg)
