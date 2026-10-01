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
error-too-large = Файл завеликий
error-rate-limited = Забагато запитів. Зачекайте трохи й спробуйте знову.
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
auth-verification-required = Спершу пройдіть перевірку нижче.
auth-verification-failed = Перевірка не вдалася. Спробуйте ще раз.
auth-verification-unavailable = Перевірка не завантажилася. Перевірте з'єднання та перезавантажте сторінку.

## Top bar and account menu

surprise-me = Здивуй мене
surprise-me-title = Випадковий запис зі сховища для натхнення
surprise-me-disabled = Спершу щось збережіть
surprise-me-nothing = Поки немає нічого, чим можна здивувати.
surprise-me-failed = Не вдалося вибрати випадковий запис: { $error }

display-mode-label = Режим:
display-mode-normal = Нормальний
display-mode-blind = Сліпий
display-mode-normal-title = Показувати все
display-mode-blind-title = Не показувати записи з прихованими тегами
account-menu = Обліковий запис
account-settings = Налаштування облікового запису
account-logout = Вийти

## Capture box

home-title = Ви зберігаєте. Ми не даємо забути.
home-tagline = Більше ніяких збережених, які ніхто не відкриває
home-drop-hint = Відпустіть файли, щоб завантажити
home-input-placeholder = Вставте посилання, додайте файл або напишіть нотатку...
home-remove-file = Прибрати файл
home-attach = Прикріпити зображення або файли (або вставте їх сюди)
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
nav-media = Медіа
nav-all-media = Усі медіа
nav-images = Зображення
nav-video = Відео
nav-audio = Аудіо
nav-links = Посилання
nav-files = Файли
nav-all-files = Усі файли
nav-documents = Документи
nav-books = Книги
nav-other-files = Інше
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

## Sorting

sort-newest = Спочатку нові
sort-oldest = Спочатку старі
sort-random = Випадково
sort-title = Сортування: { $order }
sort-reshuffle = Перемішати ще раз
sort-search-relevance = Результати пошуку впорядковано за релевантністю

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

## Collections

collections-label = Колекції
collections-add = Колекція
collections-add-title = Додати до колекцій
collections-remove = Прибрати з колекції
collections-remove-named = Прибрати з колекції { $name }
collections-search-placeholder = Пошук колекцій...
collections-none-yet = Колекцій поки немає — створіть першу під час збереження чи редагування запису.
collections-no-matches = Немає відповідних колекцій
collections-load-failed = Не вдалося завантажити колекції: { $error }
collections-create = + Створити колекцію «{ $name }»

## Filters (collections and tags)

filters-label = Фільтри
filters-refine-search = Показано лише перші — скористайтеся пошуком, щоб знайти інші.

## Items

items-today = Сьогодні
items-yesterday = Вчора

item-edit = Редагувати
item-delete = Видалити
item-more-actions = Інші дії
item-like = Додати в обране
item-unlike = Прибрати з обраного
item-image-alt = Збережене зображення
item-image-viewer = Перегляд зображення
item-viewer = Запис
item-video-viewer = Відеоплеєр
item-video-open = Відтворити відео: { $name }
item-video-loading = Завантаження відео…
item-video-unsupported = Цей формат відео не відтворюється у вашому браузері.
item-video-failed = Не вдалося завантажити відео.
item-media-retry = Повторити
media-load-failed = Не вдалося завантажити
file-open-failed = Не вдалося отримати файл. Спробуйте ще раз.
item-audio-viewer = Аудіоплеєр
item-audio-open = Відтворити аудіо: { $name }
item-audio-loading = Завантаження аудіо…
item-audio-unsupported = Цей формат аудіо не відтворюється у вашому браузері.
item-audio-failed = Не вдалося завантажити аудіо.
item-field-filename = Назва файлу
item-field-text = Текст
item-field-caption = Підпис
item-field-tags = Теги
item-field-collections = Колекції
item-field-type = Тип
item-caption-placeholder = Додайте підпис
item-save-as = Зберегти як
item-type-text = Текст
item-type-link = Посилання
item-previous = Попередній елемент
item-next = Наступний елемент

## File sizes ($size is already formatted for the language)

size-bytes = { $size } Б
size-kilobytes = { $size } КБ
size-megabytes = { $size } МБ

## Settings

settings-title = Налаштування
settings-section-password = Пароль
settings-section-language = Мова
settings-section-hidden-tags = Приховані теги
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
settings-hidden-tags-description = У сліпому режимі записи з будь-яким із позначених тегів не показуються. Ваш вибір запам’ятовується на цьому пристрої.
settings-hidden-tags-limit = Можна приховати щонайбільше { $max } тегів.
settings-language-label = Мова
settings-section-delete-account = Видалити акаунт
settings-delete-account-description = Остаточно видаліть свій акаунт і все, що в ньому є: усі записи й файли, теги та колекції. Цю дію не можна скасувати. Ви вийдете з усіх пристроїв.
settings-delete-account-password = Пароль
settings-delete-account-password-missing = Введіть пароль
settings-delete-account = Видалити акаунт
settings-delete-account-confirm-title = Видалити акаунт?
settings-delete-account-confirm-message = Усе, що ви зберегли в Stash, буде видалено назавжди. Цю дію не можна скасувати.
settings-delete-account-confirm = Видалити назавжди
settings-deleting-account = Видалення…

## Вибір записів (прямокутником по картках) і дії з усіма вибраними

selection-count = { $count } вибрано
selection-select = Вибрати
selection-deselect = Зняти вибір
selection-cancel-title = Скасувати вибір (Esc)
selection-tags-title = Додати або прибрати теги у вибраних записів
selection-collections-title = Додати вибрані записи до колекцій або прибрати з них
selection-favorite-add = Додати вибрані записи до обраного
selection-favorite-remove = Прибрати вибрані записи з обраного
selection-delete-title = Видалити вибрані записи
selection-failed = Не вдалося змінити деякі записи: { $error }

## Підтвердження видалення

delete-confirm-title = { $count ->
        [1] Видалити цей запис?
       *[other] Видалити { $count } { $count ->
            [one] запис
            [few] записи
           *[many] записів
        }?
    }
delete-confirm-message = Цю дію не можна скасувати.
delete-confirm-deleting = Видалення…
delete-failed-some = Не вдалося видалити деякі записи: { $error }

## Дублікати під час завантаження (така сама назва, тип і розмір, як у збереженого файлу або іншого файлу цього завантаження)

duplicates-title = { $count ->
        [1] Цей файл — дублікат
       *[other] Деякі файли — дублікати
    }
duplicates-message-one = Ви вже зберігали файл «{ $name }».
duplicates-message-many = Ці файли мають таку саму назву й розмір, як уже збережені або як інший файл, що ви завантажуєте. Інші файли завантажаться як звичайно.
duplicates-message-one-in-batch = Ви завантажуєте ще один файл «{ $name }» точно такого самого розміру.
duplicates-same-in-batch = Такий самий, як «{ $name }» у цьому завантаженні
duplicates-copies = { $count ->
        [one] Знайдено { $count } копію
        [few] Знайдено { $count } копії
       *[many] Знайдено { $count } копій
    }
duplicates-first-saved = Уперше завантажено { $date }
duplicates-last-saved = Остання копія { $date }
duplicates-skip = Пропустити
duplicates-upload-another = Завантажити ще одну копію
duplicates-upload-copy = Завантажити копію
duplicates-skip-all = Пропустити всі
duplicates-upload-all = Завантажити всі копії
duplicates-continue = Продовжити
duplicates-check-failed = Не вдалося перевірити дублікати: { $error }
