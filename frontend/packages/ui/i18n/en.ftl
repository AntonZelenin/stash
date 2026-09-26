# English UI strings: the reference set of keys. Every other language
# must define exactly these keys (checked by the `i18n` tests); anything a
# translation lacks falls back to the English below.

app-name = stash

## Common

common-cancel = Cancel
common-save = Save
common-saving = Saving…
common-close = Close
common-loading = Loading...

## Errors

error-network = Could not reach the server
error-unauthorized = Invalid credentials
error-conflict = That email is already registered
error-invalid-request = Invalid request
error-server = Something went wrong

## Login and signup

auth-tagline-save = Save everything.
auth-tagline-find = Find it anytime.
auth-login = Log in
auth-logging-in = Logging in...
auth-signup = Sign up
auth-signing-up = Signing up...
auth-email = Email
auth-password = Password
auth-confirm-password = Confirm password
auth-forgot-password = Forgot password?
auth-email-missing = Enter your email
auth-email-invalid = Enter a valid email address
auth-password-missing = Enter your password
auth-password-too-short = Password must be at least { $min } { $min ->
        [one] character
       *[other] characters
    }
auth-password-too-long = Password must be at most { $max } { $max ->
        [one] character
       *[other] characters
    }
auth-passwords-mismatch = Passwords don't match

## Top bar and account menu

surprise-me = Surprise me
surprise-me-title = Inspire me with a random stash item
surprise-me-disabled = Save something first
surprise-me-nothing = Nothing saved yet to surprise you with.
surprise-me-failed = Could not pick a random item: { $error }
account-menu = Account
account-settings = Account settings
account-logout = Logout

## Capture box

home-title = Save anything. Find anytime.
home-tagline = Notes, links, media, and files — all in one place.
home-drop-hint = Drop files to upload
home-input-placeholder = Paste a link, drag an image, or type a fleeting thought...
home-remove-file = Remove file
home-attach = Attach images or files (or paste them here)
home-submit = Stash
home-read-failed = Could not read the selected file

## Item list

home-loading = Loading your stash...
home-load-failed = Could not load your stash: { $error }
home-empty = Nothing saved yet — items you capture will show up here.
home-no-filter-matches = Nothing matches these filters.
home-delete-failed = Could not delete the item: { $error }

## Type and favorites filters

nav-all-items = All Items
nav-text-notes = Text & Notes
nav-images = Images & Media
nav-links = Links
nav-files = Files
nav-favorites = Favorites
nav-favorites-only-on = Showing favorites only
nav-favorites-only-off = Show favorites only

## Search

search-placeholder = Search naturally...
search-clear = Clear search
search-searching = Searching...
search-failed = Search failed: { $error }
search-no-results = Nothing matches “{ $query }”.

## Date filter

date-label = Date
date-clear = Clear date filter
date-pick-year = Pick a year
date-no-items = Nothing saved yet.
date-years-failed = Could not load dates: { $error }
date-apply = Filter
date-previous = Previous
date-next = Next
# Standalone month name ($month is 1-12), as in "March 2025".
date-month = { $month ->
    [1] January
    [2] February
    [3] March
    [4] April
    [5] May
    [6] June
    [7] July
    [8] August
    [9] September
    [10] October
    [11] November
   *[12] December
}

## Sorting

sort-newest = Newest first
sort-oldest = Oldest first
sort-random = Random
sort-title = Sort: { $order }
sort-reshuffle = Shuffle again
sort-search-relevance = Search results are sorted by relevance

## Tags

tags-label = Tags
tags-add = Add tag
tags-add-title = Add tags
tags-add-button = + Add tag
tags-remove = Remove tag
tags-remove-named = Remove tag { $name }
tags-remove-filter = Remove filter
tags-remove-filter-named = Remove { $name } filter
tags-search-placeholder = Search tags...
tags-none-yet = No tags yet — add some on your items.
tags-no-matches = No matching tags
tags-load-failed = Could not load tags: { $error }
tags-suggested = Suggested
tags-suggested-label = Suggested:
tags-name-placeholder = Tag name
tags-create = Create “{ $name }”
tags-show-tagged = Show items tagged { $name }

## Items

item-edit = Edit
item-delete = Delete
item-more-actions = More actions
item-like = Add to favorites
item-unlike = Remove from favorites
item-image-alt = Saved image
item-image-viewer = Image viewer
item-viewer = Item
item-field-filename = Filename
item-field-text = Text
item-field-caption = Caption
item-field-tags = Tags
item-field-type = Type
item-caption-placeholder = Add a caption
item-save-as = Save as
item-type-text = Text
item-type-link = Link

## File sizes ($size is already formatted for the language)

size-bytes = { $size } B
size-kilobytes = { $size } KB
size-megabytes = { $size } MB

## Settings

settings-title = Settings
settings-section-password = Password
settings-section-language = Language
settings-password-description = Change the password you use to log in. You'll need your current password. Changing it signs you out on every other device.
settings-password-changed = Your password has been changed.
settings-current-password = Current password
settings-current-password-missing = Enter your current password
settings-new-password = New password
settings-new-password-hint = At least { $min } { $min ->
        [one] character
       *[other] characters
    }.
settings-confirm-new-password = Confirm new password
settings-change-password = Change password
settings-language-description = Choose the language Stash is shown in. Your choice is remembered on this device.
settings-language-label = Language
