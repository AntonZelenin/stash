//! What a note's/link's text is made of, detected with the same rules as
//! the server (`app.items.services.text_content_kind`), so the UI offers
//! the Text/Link choice exactly when the server will honour it. The item's
//! stored type is what's displayed and filtered on; this only decides
//! whether to ask.

/// Punctuation around a URL in prose ("(see https://x.com).") that isn't
/// part of it.
const URL_OPENERS: &[char] = &['(', '[', '<', '{', '"', '\''];
const URL_CLOSERS: &[char] = &[')', ']', '>', '}', '"', '\'', '.', ',', ';', ':', '!', '?'];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum TextKind {
    /// The whole text is one http(s) URL: always a link.
    UrlOnly,
    /// No URLs: always a note.
    NoUrl,
    /// Text with URLs, or several URLs: the user chooses.
    Mixed,
}

pub(crate) fn text_kind(text: &str) -> TextKind {
    let words: Vec<&str> = text.split_whitespace().collect();
    if let [word] = words.as_slice()
        && is_url(word)
    {
        return TextKind::UrlOnly;
    }
    if words.iter().any(|word| is_url(strip_punctuation(word))) {
        TextKind::Mixed
    } else {
        TextKind::NoUrl
    }
}

/// The first URL in `text`, without punctuation around it; what a link
/// item points to.
pub(crate) fn first_url(text: &str) -> Option<&str> {
    text.split_whitespace()
        .map(strip_punctuation)
        .find(|word| is_url(word))
}

/// A piece of text as shown with its links clickable.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Segment<'a> {
    Text(&'a str),
    Url(&'a str),
}

/// `text` cut into plain text and URLs (found as `first_url` finds them,
/// punctuation around them left as text). Joined back together, the
/// segments are exactly `text`, whitespace included.
pub(crate) fn link_segments(text: &str) -> Vec<Segment<'_>> {
    let mut segments = Vec::new();
    // Start of the text not yet emitted.
    let mut plain_start = 0;
    let mut rest = text;
    while let Some(word_start) = rest.find(|c: char| !c.is_whitespace()) {
        let word_len = rest[word_start..]
            .find(char::is_whitespace)
            .unwrap_or(rest.len() - word_start);
        let word = &rest[word_start..word_start + word_len];
        let url = strip_punctuation(word);
        if is_url(url) {
            let offset = text.len() - rest.len() + word_start;
            let url_start = offset + (word.len() - word.trim_start_matches(URL_OPENERS).len());
            if url_start > plain_start {
                segments.push(Segment::Text(&text[plain_start..url_start]));
            }
            segments.push(Segment::Url(url));
            plain_start = url_start + url.len();
        }
        rest = &rest[word_start + word_len..];
    }
    if plain_start < text.len() {
        segments.push(Segment::Text(&text[plain_start..]));
    }
    segments
}

fn strip_punctuation(word: &str) -> &str {
    word.trim_start_matches(URL_OPENERS)
        .trim_end_matches(URL_CLOSERS)
}

/// An http(s) URL with a host, matched case-insensitively on the scheme,
/// like Python's `urlsplit` check on the server.
fn is_url(word: &str) -> bool {
    let Some((scheme, rest)) = word.split_once("://") else {
        return false;
    };
    let host = rest.split(['/', '?', '#']).next().unwrap_or("");
    matches!(scheme.to_ascii_lowercase().as_str(), "http" | "https") && !host.is_empty()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_the_same_kinds_as_the_server() {
        for (text, kind) in [
            ("https://example.com", TextKind::UrlOnly),
            ("HTTP://Example.com/a?b=1#c", TextKind::UrlOnly),
            ("just a note", TextKind::NoUrl),
            ("example.com", TextKind::NoUrl),
            ("ftp://example.com", TextKind::NoUrl),
            ("http://", TextKind::NoUrl),
            ("see https://example.com", TextKind::Mixed),
            ("(https://example.com).", TextKind::Mixed),
            ("https://example.com https://example.org", TextKind::Mixed),
            ("https://example.com\nnotes", TextKind::Mixed),
        ] {
            assert_eq!(text_kind(text), kind, "{text:?}");
        }
    }

    #[test]
    fn first_url_skips_prose_and_punctuation() {
        assert_eq!(
            first_url("read (https://example.com/a). later"),
            Some("https://example.com/a")
        );
        assert_eq!(first_url("no links"), None);
    }

    #[test]
    fn link_segments_split_out_urls_and_keep_everything_else() {
        use Segment::{Text, Url};
        assert_eq!(
            link_segments("read (https://example.com/a).\n  then http://x.org"),
            vec![
                Text("read ("),
                Url("https://example.com/a"),
                Text(").\n  then "),
                Url("http://x.org"),
            ]
        );
        assert_eq!(link_segments("no links"), vec![Text("no links")]);
        assert_eq!(link_segments(""), vec![]);
    }
}
