# Visual Regression — проверка визуальных изменений сайта

Скрипт сравнивает скриншоты эталонного сайта и сайта с изменениями
в десктопном (1440px) и мобильном (360px) разрешениях. Поддерживает
мокирование бэкенд-запросов, чтобы состояния страниц были
детерминированными. Результат — HTML-отчёт с расхождениями и
скриншотами.

---

## Работа с проектом

### Войти в проект

```bash
cd visual_regress
source .venv/bin/activate
```

После этого в приглашении появится префикс `(.venv)`:

```
(.venv) user@host:~/visual_regress$
```

Проверка, что активен именно venv:

```bash
which python     # должен показать .venv/bin/python
which pip        # должен показать .venv/bin/pip
```

Для других оболочек:

- fish: `source .venv/bin/activate.fish`
- PowerShell: `.venv\Scripts\Activate.ps1`

### Запустить проверку

```bash
# базовый запуск с config.yaml
python run_check.py

# или с другим конфигом
python run_check.py config.staging.yaml
```

Скрипт создаст папку `reports/<timestamp>/` с HTML-отчётом:

```
reports/20260115_143022/
├── index.html                    ← открыть в браузере
├── home__desktop__ref.png
├── home__desktop__cur.png
├── home__desktop__diff.png
└── ...
```

Открыть отчёт:

```bash
xdg-open reports/*/index.html      # Linux
open reports/*/index.html          # macOS
```

Код возврата:

- `0` — расхождений нет
- `1` — есть расхождения (удобно для CI)

### Выйти из проекта

```bash
deactivate
```

Префикс `(.venv)` исчезнет, ты снова в системном Python.

---

## Требования

- Linux (Debian/Ubuntu) или macOS
- Python 3.12+ (проверено на 3.14)
- ~500 MB свободного места (venv + Chromium)

## Первичная установка

```bash
# 1. Системные зависимости
sudo apt update
sudo apt install -y python3-venv python3-full \
                    libjpeg-dev zlib1g-dev libtiff-dev \
                    libfreetype6-dev libwebp-dev

# 2. Виртуальное окружение
cd visual_regress
python3 -m venv .venv
source .venv/bin/activate

# 3. Обновление pip
pip install --upgrade pip setuptools wheel

# 4. Python-зависимости
pip install -r requirements.txt

# 5. Браузер для Playwright
playwright install chromium
playwright install-deps chromium

# 6. Зафиксировать версии
pip freeze > requirements.lock.txt
```

## Проверка установки

```bash
python -c "import PIL; print('Pillow', PIL.__version__)"
python -c "import playwright; print('Playwright OK')"
python -c "from greenlet import greenlet; print('greenlet OK')"
```

Ожидаемый результат:

```
Pillow 12.3.0
Playwright OK
greenlet OK
```

---

## requirements.txt

```txt
playwright>=1.51.0
pixelmatch==0.3.0
Pillow>=11.0.0
PyYAML==6.0.2
Jinja2==3.1.4
```

Версии подобраны под Python 3.14. Для более старых Python (3.10–3.12)
можно использовать фиксированные версии:

```txt
playwright==1.47.0
pixelmatch==0.3.0
Pillow==10.4.0
PyYAML==6.0.2
Jinja2==3.1.4
```

---

## Решение проблем

### `error: externally-managed-environment`

Debian/Ubuntu блокируют установку пакетов в системный Python (PEP 668).
Решение — использовать venv. **Не используйте** `--break-system-packages`.

### `The headers or library files could not be found for jpeg`

Pillow собирается из исходников и не находит libjpeg:

```bash
sudo apt install -y libjpeg-dev zlib1g-dev libtiff-dev \
                    libfreetype6-dev libwebp-dev
```

### `greenlet` не собирается на Python 3.14

Версия greenlet из старых Playwright не поддерживает внутренние
структуры CPython 3.14 (`_PyCFrame`, `_PyInterpreterFrame`).
Обновите Playwright до >=1.51.0 — greenlet подтянется совместимый.

### `playwright: command not found`

Команда доступна только внутри активированного venv:

```bash
source .venv/bin/activate
playwright install chromium
```

### Ошибки про системные библиотеки для Chromium

```bash
playwright install-deps chromium
```

### Если ничего не помогает — Python 3.12

Более предсказуемая база для Playwright/Pillow:

```bash
sudo apt install -y python3.12 python3.12-venv
deactivate 2>/dev/null
rm -rf .venv
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
```

---

## Структура проекта

```
visual_regress/
├── .venv/                    # виртуальное окружение (не коммитить)
├── config.yaml               # URL-ы, viewport-ы, пороги, моки
├── mocks/
│   ├── basket.json
│   └── lk.json
├── run_check.py              # основной скрипт
├── requirements.txt
├── requirements.lock.txt     # зафиксированные версии (pip freeze)
├── reports/                  # отчёты (не коммитить)
└── README.md
```

## .gitignore

```
.venv/
reports/
__pycache__/
*.pyc
```
