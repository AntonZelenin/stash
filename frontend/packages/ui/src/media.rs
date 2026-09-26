//! Video and audio items: their card previews, and the native `<video>` /
//! `<audio>` player shown in their `ItemView`.

use dioxus::prelude::*;
use dioxus_i18n::t;

use crate::AuthSession;
use crate::icons::{IconMusic, IconPlay, IconVideo};

// `MediaError.code` values (0: the element has no error).
const MEDIA_ERR_ABORTED: u16 = 1;
const MEDIA_ERR_DECODE: u16 = 3;
const MEDIA_ERR_SRC_NOT_SUPPORTED: u16 = 4;

/// Backstop against refetching forever: a URL has to fail after having
/// worked for the player to fetch a new one, which with URLs lasting hours
/// should happen a few times per playback at most.
const MAX_URL_REFRESHES: u32 = 10;

/// What a `MediaPlayer` plays.
#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum MediaKind {
    Video,
    Audio,
}

impl MediaKind {
    fn loading(self) -> String {
        match self {
            Self::Video => t!("item-video-loading"),
            Self::Audio => t!("item-audio-loading"),
        }
    }

    fn failed(self) -> String {
        match self {
            Self::Video => t!("item-video-failed"),
            Self::Audio => t!("item-audio-failed"),
        }
    }

    fn unsupported(self) -> String {
        match self {
            Self::Video => t!("item-video-unsupported"),
            Self::Audio => t!("item-audio-unsupported"),
        }
    }
}

/// A video's card preview: its thumbnail if it has one; otherwise an early
/// frame of the video itself (from `video_url`), drawn by the browser, over
/// a generic video placeholder that shows where it can't decode the
/// format. A play badge sits on top; below, its filename and type/size,
/// and the caption if one was added. Clicking it (or Enter or Space on it)
/// calls `on_open`, like an image's preview.
#[component]
pub(crate) fn VideoBody(
    thumbnail_url: Option<String>,
    video_url: Option<String>,
    filename: String,
    details: String,
    caption: Option<String>,
    on_open: EventHandler<()>,
) -> Element {
    rsx! {
        OpenButton {
            class: "item-card-video-main",
            label: t!("item-video-open", name: filename.as_str()),
            title: filename.clone(),
            on_open,
            div { class: "item-card-video-poster",
                if let Some(url) = thumbnail_url {
                    img { src: "{url}", alt: "", loading: "lazy" }
                } else {
                    if let Some(url) = video_url {
                        // Just the frame at 0.1s (the very first is often
                        // black): only its metadata and that frame are
                        // fetched, in ranges, and it never plays here.
                        video {
                            src: "{url}#t=0.1",
                            preload: "metadata",
                            muted: true,
                            playsinline: true,
                            tabindex: "-1",
                            aria_hidden: "true",
                        }
                    }
                    span { class: "item-card-video-placeholder", IconVideo {} }
                }
                span { class: "item-card-video-play", IconPlay {} }
            }
            span { class: "item-card-video-body",
                span { class: "item-card-file-name", "{filename}" }
                span { class: "item-card-file-details", "{details}" }
            }
            if let Some(caption) = caption {
                span { class: "item-card-file-caption item-card-video-caption", "{caption}" }
            }
        }
    }
}

/// An audio file's card: a music icon, its filename, and its type, size
/// and duration, plus the caption if one was added. Clicking it (or Enter
/// or Space on it) calls `on_open`, like a video's.
///
/// The duration isn't stored anywhere: the browser reads it from the
/// file's metadata (`audio_url`, fetched in ranges), and it shows once
/// known. It's left out where the browser can't read the format.
#[component]
pub(crate) fn AudioBody(
    item_id: String,
    audio_url: Option<String>,
    filename: String,
    details: String,
    caption: Option<String>,
    on_open: EventHandler<()>,
) -> Element {
    let element_id = use_hook(|| format!("stash-audio-card-{item_id}"));
    let mut duration = use_signal(|| None::<String>);
    let details = match duration() {
        Some(duration) => format!("{details} · {duration}"),
        None => details,
    };
    let read_duration = {
        let element_id = element_id.clone();
        move |_| {
            let element_id = element_id.clone();
            async move {
                duration.set(media_duration(&element_id).await.and_then(format_duration));
            }
        }
    };

    rsx! {
        OpenButton {
            class: "item-card-audio-main",
            label: t!("item-audio-open", name: filename.as_str()),
            title: filename.clone(),
            on_open,
            span { class: "item-card-file-row",
                span { class: "item-card-file-icon item-card-audio-icon", IconMusic {} }
                span { class: "item-card-file-body",
                    span { class: "item-card-file-name", "{filename}" }
                    span { class: "item-card-file-details", "{details}" }
                }
            }
            if let Some(caption) = caption {
                span { class: "item-card-file-caption", "{caption}" }
            }
            if let Some(url) = audio_url {
                // Never shown (no controls) nor played: only for its
                // duration.
                audio {
                    id: "{element_id}",
                    src: "{url}",
                    preload: "metadata",
                    aria_hidden: "true",
                    onloadedmetadata: read_duration.clone(),
                    ondurationchange: read_duration,
                }
            }
        }
    }
}

/// A card's clickable content, as a button opening the item's viewer
/// (`on_open`), with Enter and Space too.
#[component]
fn OpenButton(
    class: &'static str,
    label: String,
    title: String,
    on_open: EventHandler<()>,
    children: Element,
) -> Element {
    rsx! {
        div {
            class,
            role: "button",
            tabindex: "0",
            title,
            aria_label: label,
            onclick: move |_| on_open.call(()),
            onkeydown: move |evt| {
                if evt.key() == Key::Enter || evt.key() == Key::Character(" ".into()) {
                    evt.prevent_default();
                    on_open.call(());
                }
            },
            {children}
        }
    }
}

/// `seconds` as "3:07", or "1:02:03" from an hour; None if it isn't a
/// usable length (unknown, or a live stream's infinity).
fn format_duration(seconds: f64) -> Option<String> {
    if !seconds.is_finite() || seconds <= 0.0 {
        return None;
    }
    let total = seconds.round() as u64;
    let (hours, minutes, secs) = (total / 3600, total / 60 % 60, total % 60);
    Some(if hours > 0 {
        format!("{hours}:{minutes:02}:{secs:02}")
    } else {
        format!("{minutes}:{secs:02}")
    })
}

/// Why the player shows a message instead of playing.
#[derive(Clone, Copy, Debug, PartialEq)]
enum MediaProblem {
    /// The browser can't play the file's container or codec.
    Unsupported,
    /// The file couldn't be loaded (network, or the item is gone); worth
    /// trying again.
    Failed,
}

/// What to do about a playback error.
#[derive(Debug, PartialEq)]
enum ErrorAction {
    Ignore,
    /// Most likely the pre-signed URL expired (or the credentials that
    /// signed it did): fetch a new one and carry on.
    RefreshUrl,
    Show(MediaProblem),
}

/// `code` is the element's `MediaError.code`; `url_is_fresh` whether its
/// URL was issued since the media last loaded, so an error with it can't
/// be expiry.
///
/// A URL that already worked is always refreshed first, whatever the
/// error: an expired URL fails however the browser happens to report it
/// (Chrome reports a refused *first* request as "not supported"). Only a
/// fresh URL's error is final, and then decode/format errors mean the
/// format can't be played here.
fn error_action(code: u16, url_is_fresh: bool, refreshes: u32) -> ErrorAction {
    match code {
        // No error any more (it belonged to a source already replaced), or
        // a load the browser aborted itself.
        0 | MEDIA_ERR_ABORTED => ErrorAction::Ignore,
        _ if !url_is_fresh && refreshes < MAX_URL_REFRESHES => ErrorAction::RefreshUrl,
        MEDIA_ERR_DECODE | MEDIA_ERR_SRC_NOT_SUPPORTED if url_is_fresh => {
            ErrorAction::Show(MediaProblem::Unsupported)
        }
        _ => ErrorAction::Show(MediaProblem::Failed),
    }
}

/// Where playback was, to continue from after switching URLs.
#[derive(Clone, Copy, Debug, PartialEq)]
struct Position {
    seconds: f64,
    playing: bool,
}

/// The video or audio (`kind`) of item `item_id`, in the browser's own
/// player (`controls`, so seeking, volume, fullscreen and ranged requests
/// are all native). It never autoplays; a video fits the viewer at its
/// aspect ratio, with `poster` (its thumbnail) until it plays.
///
/// Its URL is fetched fresh when the player opens, and lasts a playback
/// session. If playback later fails with it (expired), a new one is fetched
/// and playback continues from where it was, playing again if it was
/// playing (as far as the browser allows). A format the browser can't play
/// shows a message instead; the original file stays available from the
/// viewer's panel. Whether a format plays is only known by trying:
/// `canPlayType` answers "" for e.g. MKV and MOV that browsers often play.
#[component]
pub(crate) fn MediaPlayer(
    item_id: String,
    kind: MediaKind,
    #[props(default)] poster: Option<String>,
) -> Element {
    let session = use_context::<AuthSession>();
    // Scripts below find the element by it. One viewer is open at a time.
    let element_id = use_hook(|| format!("stash-media-{item_id}"));
    let mut src = use_signal(|| None::<String>);
    let mut problem = use_signal(|| None::<MediaProblem>);
    let mut url_is_fresh = use_signal(|| true);
    let mut refreshes = use_signal(|| 0u32);
    // Applied once the next source has loaded its metadata.
    let mut resume_at = use_signal(|| None::<Position>);

    let fetch_url = use_callback(move |resume: Option<Position>| {
        let session = session.clone();
        let item_id = item_id.clone();
        spawn(async move {
            resume_at.set(resume);
            match session.playback_url(item_id).await {
                Ok(Some(playback)) => {
                    url_is_fresh.set(true);
                    problem.set(None);
                    src.set(Some(playback.url));
                }
                // Deleted meanwhile, or unreachable.
                Ok(None) | Err(_) => problem.set(Some(MediaProblem::Failed)),
            }
        });
    });
    use_hook(move || fetch_url.call(None));

    let on_error = {
        let element_id = element_id.clone();
        move |_| {
            let element_id = element_id.clone();
            async move {
                let Some((code, seconds, playing)) = media_state(&element_id).await else {
                    return;
                };
                let position = Position { seconds, playing };
                match error_action(code, url_is_fresh(), refreshes()) {
                    ErrorAction::Ignore => {}
                    ErrorAction::RefreshUrl => {
                        refreshes += 1;
                        fetch_url.call(Some(position));
                    }
                    ErrorAction::Show(shown) => {
                        // Kept for "Try again".
                        resume_at.set(Some(position));
                        problem.set(Some(shown));
                    }
                }
            }
        }
    };
    let on_metadata = {
        let element_id = element_id.clone();
        move |_| {
            if let Some(position) = resume_at.write().take() {
                resume(&element_id, position);
            }
        }
    };
    // It has worked with this URL: a later error may be expiry.
    let on_loaded = move |_| url_is_fresh.set(false);
    let status_class = match kind {
        MediaKind::Video => "lightbox-media-status",
        MediaKind::Audio => "lightbox-media-status lightbox-media-status-audio",
    };

    match (problem(), src()) {
        (Some(MediaProblem::Unsupported), _) => rsx! {
            div { class: status_class, role: "status",
                p { {kind.unsupported()} }
            }
        },
        (Some(MediaProblem::Failed), _) => rsx! {
            div { class: status_class, role: "alert",
                p { {kind.failed()} }
                button {
                    class: "lightbox-media-retry",
                    r#type: "button",
                    onclick: move |_| {
                        problem.set(None);
                        // Not the old URL again: loading shows until the
                        // new one arrives.
                        src.set(None);
                        fetch_url.call(resume_at());
                    },
                    {t!("item-media-retry")}
                }
            }
        },
        (None, None) => rsx! {
            div { class: status_class, role: "status",
                p { {kind.loading()} }
            }
        },
        // Enough is preloaded to show the length (and a video's size); the
        // rest is fetched (in ranges) as it plays or is sought.
        (None, Some(url)) => match kind {
            MediaKind::Video => rsx! {
                video {
                    id: "{element_id}",
                    class: "lightbox-video",
                    src: "{url}",
                    poster,
                    controls: true,
                    playsinline: true,
                    preload: "metadata",
                    onerror: on_error,
                    onloadedmetadata: on_metadata,
                    onloadeddata: on_loaded,
                }
            },
            MediaKind::Audio => rsx! {
                audio {
                    id: "{element_id}",
                    class: "lightbox-audio-player",
                    src: "{url}",
                    controls: true,
                    preload: "metadata",
                    onerror: on_error,
                    onloadedmetadata: on_metadata,
                    onloadeddata: on_loaded,
                }
            },
        },
    }
}

/// An audio item's side of its `ItemView`: a large music icon, its
/// `title`, and its `MediaPlayer`.
#[component]
pub(crate) fn AudioStage(item_id: String, title: String) -> Element {
    rsx! {
        div { class: "lightbox-audio",
            span { class: "lightbox-audio-art", IconMusic {} }
            p { class: "lightbox-audio-title", title: "{title}", "{title}" }
            MediaPlayer { item_id, kind: MediaKind::Audio }
        }
    }
}

/// The media element's error code (0 if none), current time and whether
/// it's playing; None if it's no longer there. Read through a script since
/// Dioxus's media events carry no data.
async fn media_state(element_id: &str) -> Option<(u16, f64, bool)> {
    let eval = document::eval(
        r#"
        const id = await dioxus.recv();
        const media = document.getElementById(id);
        if (!media) return null;
        return [media.error ? media.error.code : 0, media.currentTime, !media.paused];
        "#,
    );
    eval.send(element_id).ok()?;
    eval.join().await.ok().flatten()
}

/// The media element's duration in seconds, as far as known; None if it's
/// no longer there or it isn't known (JSON has no NaN or infinity).
async fn media_duration(element_id: &str) -> Option<f64> {
    let eval = document::eval(
        r#"
        const id = await dioxus.recv();
        const media = document.getElementById(id);
        return media && Number.isFinite(media.duration) ? media.duration : null;
        "#,
    );
    eval.send(element_id).ok()?;
    eval.join().await.ok().flatten()
}

/// Seeks the media element to `position`, and plays it if it was playing.
/// Playing may be refused (autoplay rules); it then stays paused there.
fn resume(element_id: &str, position: Position) {
    let eval = document::eval(
        r#"
        const [id, seconds, playing] = await dioxus.recv();
        const media = document.getElementById(id);
        if (!media) return;
        if (seconds > 0) media.currentTime = seconds;
        if (playing) media.play().catch(() => {});
        "#,
    );
    let _ = eval.send((element_id, position.seconds, position.playing));
}

#[cfg(test)]
mod tests {
    use super::*;

    const MEDIA_ERR_NETWORK: u16 = 2;

    #[test]
    fn a_url_that_worked_is_refreshed_whatever_the_error() {
        for code in [
            MEDIA_ERR_NETWORK,
            MEDIA_ERR_DECODE,
            MEDIA_ERR_SRC_NOT_SUPPORTED,
        ] {
            assert_eq!(
                error_action(code, false, 0),
                ErrorAction::RefreshUrl,
                "{code}"
            );
        }
    }

    #[test]
    fn a_fresh_urls_format_error_means_unsupported() {
        for code in [MEDIA_ERR_DECODE, MEDIA_ERR_SRC_NOT_SUPPORTED] {
            assert_eq!(
                error_action(code, true, 0),
                ErrorAction::Show(MediaProblem::Unsupported),
                "{code}"
            );
        }
    }

    #[test]
    fn a_fresh_urls_network_error_can_be_retried() {
        assert_eq!(
            error_action(MEDIA_ERR_NETWORK, true, 3),
            ErrorAction::Show(MediaProblem::Failed)
        );
    }

    #[test]
    fn stale_and_aborted_errors_are_ignored() {
        assert_eq!(error_action(0, false, 0), ErrorAction::Ignore);
        assert_eq!(
            error_action(MEDIA_ERR_ABORTED, false, 0),
            ErrorAction::Ignore
        );
    }

    #[test]
    fn refreshing_stops_after_the_limit() {
        assert_eq!(
            error_action(MEDIA_ERR_NETWORK, false, MAX_URL_REFRESHES),
            ErrorAction::Show(MediaProblem::Failed)
        );
    }

    #[test]
    fn durations_read_like_a_player_shows_them() {
        assert_eq!(format_duration(7.0).as_deref(), Some("0:07"));
        assert_eq!(format_duration(187.4).as_deref(), Some("3:07"));
        assert_eq!(format_duration(599.6).as_deref(), Some("10:00"));
        assert_eq!(format_duration(3723.0).as_deref(), Some("1:02:03"));
    }

    #[test]
    fn unusable_durations_are_left_out() {
        for seconds in [0.0, -1.0, f64::NAN, f64::INFINITY] {
            assert_eq!(format_duration(seconds), None, "{seconds}");
        }
    }
}
