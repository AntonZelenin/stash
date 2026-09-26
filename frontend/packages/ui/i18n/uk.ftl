# Ukrainian UI strings. Must define exactly the keys of en.ftl.

app-name = stash

## Common

common-cancel = Скасувати
common-save = Зберегти
common-saving = Збереження…
common-close = Закрити
common-loading = Завантаження...

## Errors

error-network = Не вдалося з’єднатися із сервером
error-unauthorized = Неправильні облікові дані
error-conflict = Цю електронну пошту вже зареєстровано
error-invalid-request = Некоректний запит
error-server = Щось пішло не так

## Login and signup

auth-tagline-save = Зберігайте все.
auth-tagline-find = Знаходьте будь-коли.
auth-login = Увійти
auth-logging-in = Вхід...
auth-signup = Зареєструватися
auth-signing-up = Реєстрація...
auth-email = Електронна пошта
auth-password = Пароль
auth-confirm-password = Підтвердьте пароль
auth-forgot-password = Забули пароль?
auth-email-missing = Введіть електронну пошту
auth-email-invalid = Введіть дійсну адресу електронної пошти
auth-password-missing = Введіть пароль
auth-password-too-short = Пароль має містити щонайменше { $min } { $min ->
        [one] символ
        [few] символи
       *[many] символів
    }
auth-password-too-long = Пароль має містити не більше { $max } { $max ->
        [one] символу
       *[other] символів
    }
auth-passwords-mismatch = Паролі не збігаються

## Top bar and account menu

surprise-me = Здивуй мене
surprise-me-title = Випадковий запис зі сховища для натхнення
surprise-me-disabled = Спершу щось збережіть
surprise-me-nothing = Поки немає нічого, чим можна здивувати.
surprise-me-failed = Не вдалося вибрати випадковий запис: { $error }
account-menu = Обліковий запис
account-settings = Налаштування облікового запису
account-logout = Вийти

## Capture box

home-title = Зберігайте будь-що. Знаходьте будь-коли.
home-tagline = Нотатки, посилання, медіа та файли — усе в одному місці.
home-drop-hint = Відпустіть файли, щоб завантажити
home-input-placeholder = Вставте посилання, додайте файл або напишіть нотатку...
home-remove-file = Прибрати файл
home-attach = Прикріпити зображення або файли
home-submit = Зберегти
home-read-failed = Не вдалося прочитати вибраний файл

## Item list

home-loading = Завантаження вашого сховища...
home-load-failed = Не вдалося завантажити ваше сховище: { $error }
home-empty = Тут поки порожньо — збережені записи з’являться тут.
home-no-filter-matches = Нічого не відповідає цим фільтрам.
home-delete-failed = Не вдалося видалити запис: { $error }

## Type and favorites filters

nav-all-items = Усі записи
nav-text-notes = Тексти й нотатки
nav-images = Зображення й медіа
nav-links = Посилання
nav-files = Файли
nav-favorites = Обране
nav-favorites-only-on = Показано лише обране
nav-favorites-only-off = Показати лише обране

## Search

search-placeholder = Знайдіть за описом...
search-clear = Очистити пошук
search-searching = Пошук...
search-failed = Помилка пошуку: { $error }
search-no-results = Нічого не знайдено за запитом «{ $query }».

## Date filter

date-label = Дата
date-clear = Скинути фільтр за датою
date-pick-year = Оберіть рік
date-no-items = Ще нічого не збережено.
date-years-failed = Не вдалося завантажити дати: { $error }
date-apply = Фільтрувати
date-previous = Назад
date-next = Далі
# Називний відмінок ($month — 1-12), як у «березень 2025».
date-month = { $month ->
    [1] січень
    [2] лютий
    [3] березень
    [4] квітень
    [5] травень
    [6] червень
    [7] липень
    [8] серпень
    [9] вересень
    [10] жовтень
    [11] листопад
   *[12] грудень
}

## Tags

tags-label = Теги
tags-add = Додати тег
tags-add-title = Додати теги
tags-add-button = + Додати тег
tags-remove = Видалити тег
tags-remove-named = Видалити тег { $name }
tags-remove-filter = Прибрати фільтр
tags-remove-filter-named = Прибрати фільтр { $name }
tags-search-placeholder = Пошук тегів...
tags-none-yet = Тегів поки немає — додайте їх до своїх записів.
tags-no-matches = Немає відповідних тегів
tags-load-failed = Не вдалося завантажити теги: { $error }
tags-suggested = Рекомендовані
tags-suggested-label = Рекомендовані:
tags-name-placeholder = Назва тегу
tags-create = Створити «{ $name }»
tags-show-tagged = Показати записи з тегом { $name }

## Items

item-edit = Редагувати
item-delete = Видалити
item-more-actions = Інші дії
item-like = Додати в обране
item-unlike = Прибрати з обраного
item-image-alt = Збережене зображення
item-image-viewer = Перегляд зображення
item-viewer = Запис
item-field-filename = Назва файлу
item-field-text = Текст
item-field-caption = Підпис
item-field-tags = Теги
item-field-type = Тип
item-caption-placeholder = Додайте підпис
item-save-as = Зберегти як
item-type-text = Текст
item-type-link = Посилання

## File sizes ($size is already formatted for the language)

size-bytes = { $size } Б
size-kilobytes = { $size } КБ
size-megabytes = { $size } МБ

## Settings

settings-title = Налаштування
settings-section-password = Пароль
settings-section-language = Мова
settings-password-description = Змініть пароль, яким ви входите. Знадобиться поточний пароль. Після зміни ви вийдете з усіх інших пристроїв.
settings-password-changed = Ваш пароль змінено.
settings-current-password = Поточний пароль
settings-current-password-missing = Введіть поточний пароль
settings-new-password = Новий пароль
settings-new-password-hint = Щонайменше { $min } { $min ->
        [one] символ
        [few] символи
       *[many] символів
    }.
settings-confirm-new-password = Підтвердьте новий пароль
settings-change-password = Змінити пароль
settings-language-description = Мова, якою показано Stash. Ваш вибір запам’ятовується на цьому пристрої.
settings-language-label = Мова
