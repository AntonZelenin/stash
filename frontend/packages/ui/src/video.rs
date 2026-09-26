//! Video items: their card preview, and the native `<video>` player shown
//! in their `ItemView`.

use dioxus::prelude::*;
use dioxus_i18n::t;

use crate::AuthSession;
use crate::icons::{IconPlay, IconVideo};

// `MediaError.code` values (0: the element has no error).
const MEDIA_ERR_ABORTED: u16 = 1;
const MEDIA_ERR_DECODE: u16 = 3;
const MEDIA_ERR_SRC_NOT_SUPPORTED: u16 = 4;

/// Backstop against refetching forever: a URL has to fail after having
/// worked for the player to fetch a new one, which with URLs lasting hours
/// should happen a few times per viewing at most.
const MAX_URL_REFRESHES: u32 = 10;

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
        div {
            class: "item-card-video-main",
            role: "button",
            tabindex: "0",
            title: "{filename}",
            aria_label: t!("item-video-open", name: filename.as_str()),
            onclick: move |_| on_open.call(()),
            onkeydown: move |evt| {
                if evt.key() == Key::Enter || evt.key() == Key::Character(" ".into()) {
                    evt.prevent_default();
                    on_open.call(());
                }
            },
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

/// Why the player shows a message instead of the video.
#[derive(Clone, Copy, Debug, PartialEq)]
enum VideoProblem {
    /// The browser can't play the file's container or codec.
    Unsupported,
    /// The video couldn't be loaded (network, or the item is gone); worth
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
    Show(VideoProblem),
}

/// `code` is the element's `MediaError.code`; `url_is_fresh` whether its
/// URL was issued since the video last loaded, so an error with it can't
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
            ErrorAction::Show(VideoProblem::Unsupported)
        }
        _ => ErrorAction::Show(VideoProblem::Failed),
    }
}

/// Where playback was, to continue from after switching URLs.
#[derive(Clone, Copy, Debug, PartialEq)]
struct Position {
    seconds: f64,
    playing: bool,
}

/// The video of item `item_id`, in the browser's own player (`controls`,
/// so seeking, volume, fullscreen and ranged requests are all native). It
/// never autoplays, and fits the viewer at the video's aspect ratio.
///
/// Its URL is fetched fresh when the player opens, and lasts a viewing
/// session. If playback later fails with it (expired), a new one is fetched
/// and the video continues from where it was, playing again if it was
/// playing (as far as the browser allows). A format the browser can't play
/// shows a message instead; the original file stays available from the
/// viewer's panel. Whether a format plays is only known by trying:
/// `canPlayType` answers "" for e.g. MKV and MOV that browsers often play.
#[component]
pub(crate) fn VideoPlayer(item_id: String, poster: Option<String>) -> Element {
    let session = use_context::<AuthSession>();
    // Scripts below find the element by it. One viewer is open at a time.
    let element_id = use_hook(|| format!("stash-video-{item_id}"));
    let mut src = use_signal(|| None::<String>);
    let mut problem = use_signal(|| None::<VideoProblem>);
    let mut url_is_fresh = use_signal(|| true);
    let mut refreshes = use_signal(|| 0u32);
    // Applied once the next source has loaded its metadata.
    let mut resume_at = use_signal(|| None::<Position>);

    let fetch_url = use_callback(move |resume: Option<Position>| {
        let session = session.clone();
        let item_id = item_id.clone();
        spawn(async move {
            resume_at.set(resume);
            match session.video_url(item_id).await {
                Ok(Some(video)) => {
                    url_is_fresh.set(true);
                    problem.set(None);
                    src.set(Some(video.url));
                }
                // Deleted meanwhile, or unreachable.
                Ok(None) | Err(_) => problem.set(Some(VideoProblem::Failed)),
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

    match (problem(), src()) {
        (Some(VideoProblem::Unsupported), _) => rsx! {
            div { class: "lightbox-video-status", role: "status",
                p { {t!("item-video-unsupported")} }
            }
        },
        (Some(VideoProblem::Failed), _) => rsx! {
            div { class: "lightbox-video-status", role: "alert",
                p { {t!("item-video-failed")} }
                button {
                    class: "lightbox-video-retry",
                    r#type: "button",
                    onclick: move |_| {
                        problem.set(None);
                        // Not the old URL again: loading shows until the
                        // new one arrives.
                        src.set(None);
                        fetch_url.call(resume_at());
                    },
                    {t!("item-video-retry")}
                }
            }
        },
        (None, None) => rsx! {
            div { class: "lightbox-video-status", role: "status",
                p { {t!("item-video-loading")} }
            }
        },
        (None, Some(url)) => rsx! {
            video {
                id: "{element_id}",
                class: "lightbox-video",
                src: "{url}",
                poster,
                controls: true,
                playsinline: true,
                // Enough to show its size and duration; the rest is fetched
                // (in ranges) as it plays or is sought.
                preload: "metadata",
                onerror: on_error,
                onloadedmetadata: on_metadata,
                // It has worked with this URL: a later error may be expiry.
                onloadeddata: move |_| url_is_fresh.set(false),
            }
        },
    }
}

/// The video element's error code (0 if none), current time and whether
/// it's playing; None if it's no longer there. Read through a script since
/// Dioxus's media events carry no data.
async fn media_state(element_id: &str) -> Option<(u16, f64, bool)> {
    let eval = document::eval(
        r#"
        const id = await dioxus.recv();
        const video = document.getElementById(id);
        if (!video) return null;
        return [video.error ? video.error.code : 0, video.currentTime, !video.paused];
        "#,
    );
    eval.send(element_id).ok()?;
    eval.join().await.ok().flatten()
}

/// Seeks the video element to `position`, and plays it if it was playing.
/// Playing may be refused (autoplay rules); it then stays paused there.
fn resume(element_id: &str, position: Position) {
    let eval = document::eval(
        r#"
        const [id, seconds, playing] = await dioxus.recv();
        const video = document.getElementById(id);
        if (!video) return;
        if (seconds > 0) video.currentTime = seconds;
        if (playing) video.play().catch(() => {});
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
                ErrorAction::Show(VideoProblem::Unsupported),
                "{code}"
            );
        }
    }

    #[test]
    fn a_fresh_urls_network_error_can_be_retried() {
        assert_eq!(
            error_action(MEDIA_ERR_NETWORK, true, 3),
            ErrorAction::Show(VideoProblem::Failed)
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
            ErrorAction::Show(VideoProblem::Failed)
        );
    }
}
