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
error-too-large = The file is too large
error-rate-limited = Too many requests. Please wait a little and try again.
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
auth-verification-required = Complete the verification below first.
auth-verification-failed = Verification failed. Please try again.
auth-verification-unavailable = The verification didn't load. Check your connection and reload the page.

## Top bar and account menu

surprise-me = Surprise me
surprise-me-title = Inspire me with a random stash item
surprise-me-disabled = Save something first
surprise-me-nothing = Nothing saved yet to surprise you with.
surprise-me-failed = Could not pick a random item: { $error }

display-mode-label = Mode:
display-mode-normal = Normal
display-mode-blind = Blind
display-mode-normal-title = Show everything
display-mode-blind-title = Leave out items with hidden tags
account-menu = Account
account-settings = Account settings
account-logout = Logout

## Capture box

home-title = You save it. We make sure you don't forget it.
home-tagline = No more saves you never open again.
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
nav-media = Media
nav-all-media = All Media
nav-images = Images
nav-video = Video
nav-audio = Audio
nav-links = Links
nav-files = Files
nav-all-files = All Files
nav-documents = Documents
nav-books = Books
nav-other-files = Other
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

## Collections

collections-label = Collections
collections-add = Collection
collections-add-title = Add to collections
collections-remove = Remove from collection
collections-remove-named = Remove from collection { $name }
collections-search-placeholder = Search collections...
collections-none-yet = No collections yet — create one when saving or editing an item.
collections-no-matches = No matching collections
collections-load-failed = Could not load collections: { $error }
collections-create = + Create collection “{ $name }”

## Filters (collections and tags)

filters-label = Filters
filters-refine-search = Only the first ones are listed — search to find more.

## Items

items-today = Today
items-yesterday = Yesterday

item-edit = Edit
item-delete = Delete
item-more-actions = More actions
item-like = Add to favorites
item-unlike = Remove from favorites
item-image-alt = Saved image
item-image-viewer = Image viewer
item-viewer = Item
item-video-viewer = Video player
item-video-open = Play video: { $name }
item-video-loading = Loading video…
item-video-unsupported = This video format cannot be played in your browser.
item-video-failed = Could not load the video.
item-media-retry = Retry
media-load-failed = Couldn't load
file-open-failed = Couldn't get the file. Please try again.
item-audio-viewer = Audio player
item-audio-open = Play audio: { $name }
item-audio-loading = Loading audio…
item-audio-unsupported = This audio format cannot be played in your browser.
item-audio-failed = Could not load the audio.
item-field-filename = Filename
item-field-text = Text
item-field-caption = Caption
item-field-tags = Tags
item-field-collections = Collections
item-field-type = Type
item-caption-placeholder = Add a caption
search-note-add = Add search note
search-note-add-title = Extra words to find it by, not shown on its card
search-note-label = Search note
search-note-placeholder = Extra words to find it by (only shown when it's opened)
search-note-hint = Helps search find it. Not shown on its card.
item-save-as = Save as
item-type-text = Text
item-type-link = Link
item-previous = Previous item
item-next = Next item

## File sizes ($size is already formatted for the language)

size-bytes = { $size } B
size-kilobytes = { $size } KB
size-megabytes = { $size } MB

## Settings

settings-title = Settings
settings-section-password = Password
settings-section-language = Language
settings-section-hidden-tags = Hidden tags
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
settings-language-description = Choose the language Stash is shown in. Your choice is saved to your account and used on all your devices.
settings-hidden-tags-description = Items with any of the checked tags are left out in Blind mode. Your choice is saved to your account and used on all your devices; whether Blind mode is on is set on each device.
settings-hidden-tags-limit = At most { $max } tags can be hidden.
settings-language-label = Language
settings-section-privacy = Privacy
settings-privacy-private-title = Your data is private
settings-privacy-private-text = Your items, files, tags and collections are available only in your account. Other Stash users can't view them.
settings-privacy-openai-title = Some content is processed by OpenAI
settings-privacy-openai-text = To power search and automatic analysis, Stash may send OpenAI:
settings-privacy-openai-images = image previews;
settings-privacy-openai-videos = still frames from videos (never their sound);
settings-privacy-openai-documents = the text and names of documents;
settings-privacy-openai-notes = the text of notes, captions and links;
settings-privacy-openai-queries = search queries.
settings-privacy-account-title = OpenAI doesn't receive your account details
settings-privacy-account-text = Along with the content, Stash doesn't send OpenAI your email address, user ID or any other data that directly links a request to your account.
settings-privacy-account-text-2 = OpenAI sees that a request came from Stash, but doesn't learn which Stash user the content belongs to.
settings-privacy-retention-title = OpenAI may keep data for up to 30 days
settings-privacy-retention-text = Stash uses the OpenAI API with response storage turned off. Still, OpenAI may temporarily keep the data it receives for up to 30 days to detect abuse and ensure safety.
settings-privacy-delete-title = You can delete your data
settings-privacy-delete-text = You can delete saved items and files at any time.
settings-privacy-delete-text-2 = When you delete your account, your items, tags, collections and active sessions are removed from the main database right away. Stored files are deleted too; if storage is temporarily unavailable, the cleanup is retried automatically later.
settings-privacy-backups-title = Backups may be kept for a while
settings-privacy-backups-text = Automatic database backups are kept for up to 7 days, so deleted data may remain in them until then.
settings-privacy-backups-text-2 = Some backups made during maintenance or infrastructure changes may be kept longer.
settings-privacy-backups-text-3 = Data already sent to OpenAI can't be recalled separately, but the retention period above applies to it.
settings-privacy-analytics-title = Stash records how its features are used
settings-privacy-analytics-text = To learn which features help, Stash records actions such as opening the app, saving, opening or deleting items, searching and changing settings, with details like an item's type, a file's size or the number of search results. They're sent to PostHog under a random identifier, not your email address or user ID.
settings-privacy-analytics-text-2 = They never include your content: not the text of items, descriptions, file names, links, search queries, tag names or passwords. Your IP address isn't stored with them.
settings-section-delete-account = Delete account
settings-delete-account-description = Permanently delete your account and everything in it: all your items and files, tags and collections. This can't be undone. You'll be signed out on every device.
settings-delete-account-password = Password
settings-delete-account-password-missing = Enter your password
settings-delete-account = Delete account
settings-delete-account-confirm-title = Delete your account?
settings-delete-account-confirm-message = Everything you've saved in Stash will be deleted for good. This can't be undone.
settings-delete-account-confirm = Delete forever
settings-deleting-account = Deleting…

## Selecting items (drag a rectangle over the cards) and acting on them all

selection-count = { $count } selected
selection-select = Select
selection-deselect = Deselect
selection-cancel-title = Clear the selection (Esc)
selection-tags-title = Add or remove tags on the selected items
selection-collections-title = Add the selected items to collections or take them out
selection-favorite-add = Add the selected items to favorites
selection-favorite-remove = Remove the selected items from favorites
selection-delete-title = Delete the selected items
selection-failed = Some items could not be changed: { $error }

## Delete confirmation

delete-confirm-title = { $count ->
        [1] Delete this item?
       *[other] Delete { $count } items?
    }
delete-confirm-message = This can't be undone.
delete-confirm-deleting = Deleting…
delete-failed-some = Some items could not be deleted: { $error }

## Duplicate uploads (same name, type and size as a saved file, or as another file of the upload)

duplicates-title = { $count ->
        [1] This file is a duplicate
       *[other] Some files are duplicates
    }
duplicates-message-one = You've already saved a file named “{ $name }”.
duplicates-message-many = These files have the same name and size as files you've saved, or as another file you're uploading. The other files upload as usual.
duplicates-message-one-in-batch = You're uploading another file named “{ $name }” of exactly the same size.
duplicates-same-in-batch = Same as “{ $name }” in this upload
duplicates-copies = { $count ->
        [one] { $count } copy found
       *[other] { $count } copies found
    }
duplicates-first-saved = First uploaded { $date }
duplicates-last-saved = Most recent copy { $date }
duplicates-skip = Skip
duplicates-upload-another = Upload another copy
duplicates-upload-copy = Upload copy
duplicates-skip-all = Skip all
duplicates-upload-all = Upload all copies
duplicates-continue = Continue
duplicates-check-failed = Could not check for duplicates: { $error }
