use api::SavedYear;
use chrono::{DateTime, Datelike, Local, Months, NaiveDate, SecondsFormat, TimeZone, Utc};
use dioxus::prelude::*;
use dioxus_i18n::t;

use crate::AuthSession;
use crate::i18n::{Language, api_error_message, current_language};
use crate::icons::{IconCalendar, IconChevronDown, IconChevronLeft, IconChevronRight, IconClose};

const FILTERS_CSS: Asset = asset!("/assets/styling/filters.css");

/// Years on one page of the year grid (4 rows of 3).
const YEARS_PER_PAGE: usize = 12;

/// A saved-date filter: a whole year, a month of it, or a day of that
/// month. Applied by the server, to listing and search alike.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DateSelection {
    pub year: i32,
    pub month: Option<u32>,
    /// Only with `month`.
    pub day: Option<u32>,
}

impl DateSelection {
    fn year(year: i32) -> Self {
        Self {
            year,
            month: None,
            day: None,
        }
    }

    fn month(year: i32, month: u32) -> Self {
        Self {
            year,
            month: Some(month),
            day: None,
        }
    }

    fn day(date: NaiveDate) -> Self {
        Self {
            year: date.year(),
            month: Some(date.month()),
            day: Some(date.day()),
        }
    }

    /// The first day the selection covers.
    fn first_day(self) -> NaiveDate {
        NaiveDate::from_ymd_opt(self.year, self.month.unwrap_or(1), self.day.unwrap_or(1))
            .expect("selections are built from valid dates")
    }

    /// The day right after the last one the selection covers.
    fn day_after(self) -> NaiveDate {
        let first = self.first_day();
        match (self.month, self.day) {
            (_, Some(_)) => first.succ_opt(),
            (Some(_), None) => first.checked_add_months(Months::new(1)),
            (None, _) => first.checked_add_months(Months::new(12)),
        }
        .expect("within chrono's date range")
    }

    /// The API's `(created_from, created_before)`: where the selection
    /// starts and ends on the user's own clock, so "2025" is 2025 in their
    /// time zone rather than in UTC.
    pub fn api_range(self) -> (String, String) {
        let (from, before) = self.range_in(&Local);
        let format = |time: DateTime<Utc>| time.to_rfc3339_opts(SecondsFormat::Secs, true);
        (format(from), format(before))
    }

    fn range_in<Tz: TimeZone>(self, zone: &Tz) -> (DateTime<Utc>, DateTime<Utc>) {
        (
            start_of_day(self.first_day(), zone),
            start_of_day(self.day_after(), zone),
        )
    }

    /// "2025", "March 2025" or "5 March 2025", in the UI language. Needs
    /// `t!`, so call it under `use_init_localization`.
    fn label(self, language: Language) -> String {
        match (self.month, self.day) {
            (Some(_), Some(_)) => self
                .first_day()
                .format_localized("%-d %B %Y", language.chrono_locale())
                .to_string(),
            (Some(month), None) => month_year_label(self.year, month),
            (None, _) => self.year.to_string(),
        }
    }
}

/// When `date` starts in `zone`: midnight, or if a clock change skips
/// midnight there, the first hour that exists.
fn start_of_day<Tz: TimeZone>(date: NaiveDate, zone: &Tz) -> DateTime<Utc> {
    (0..24)
        .find_map(|hour| {
            zone.from_local_datetime(&date.and_hms_opt(hour, 0, 0)?)
                .earliest()
        })
        .expect("every day has an hour that exists")
        .with_timezone(&Utc)
}

/// "March 2025". The month name comes from the translations rather than
/// chrono, whose Ukrainian `%B` is the genitive ("березня"), right only
/// after a day number.
fn month_year_label(year: i32, month: u32) -> String {
    format!("{} {year}", t!("date-month", month: month))
}

/// The years in `zone` with items, oldest first. Each of the server's
/// years becomes the years its first and last save fall in here, and any
/// between: a year elsewhere overlaps at most two of ours, and both ends
/// are real saves, so every year this gives has items.
fn local_years<Tz: TimeZone>(saved: &[SavedYear], zone: &Tz) -> Vec<i32> {
    let year_in_zone = |time: &str| {
        DateTime::parse_from_rfc3339(time)
            .ok()
            .map(|time| time.with_timezone(zone).year())
    };
    let mut years: Vec<i32> = saved
        .iter()
        .filter_map(|saved| {
            Some(year_in_zone(&saved.first_saved_at)?..=year_in_zone(&saved.last_saved_at)?)
        })
        .flatten()
        .collect();
    years.sort_unstable();
    years.dedup();
    years
}

/// The year-grid page (0 = newest) showing `year`, with `years` (oldest
/// first) split into pages of `YEARS_PER_PAGE` from the newest end. The
/// newest page if `year` has no items.
fn page_of(years: &[i32], year: i32) -> usize {
    years
        .iter()
        .position(|&other| other == year)
        .map_or(0, |index| (years.len() - 1 - index) / YEARS_PER_PAGE)
}

/// Which grid the open calendar shows.
#[derive(Clone, Copy, PartialEq)]
enum Stage {
    Years,
    Months,
    Days,
}

/// `[ 📅 March 2025 × ▾ ]`: the saved-date filter. The panel drills down
/// from years to months to days; Filter applies whatever is picked so
/// far, so a year alone, or a year and month, are filters too.
#[component]
pub fn DateFilter(value: Signal<Option<DateSelection>>) -> Element {
    let mut open = use_signal(|| false);
    let mut value = value;

    let label = match value() {
        Some(selection) => selection.label(current_language()),
        None => t!("date-label"),
    };

    rsx! {
        document::Link { rel: "stylesheet", href: FILTERS_CSS }

        div { class: "filter-control date-filter",
            // A div rather than a button: it contains the × button, and
            // buttons can't nest.
            div {
                class: if value().is_some() { "filter-button filter-button-active" } else { "filter-button" },
                role: "button",
                tabindex: "0",
                onclick: move |_| open.toggle(),
                onkeydown: move |evt| {
                    if evt.key() == Key::Enter || evt.key() == Key::Character(" ".into()) {
                        evt.prevent_default();
                        open.toggle();
                    }
                },
                IconCalendar {}
                span { class: "filter-button-label", "{label}" }
                if value().is_some() {
                    button {
                        class: "date-filter-clear",
                        r#type: "button",
                        title: t!("date-clear"),
                        aria_label: t!("date-clear"),
                        onclick: move |evt| {
                            // Don't also toggle the dropdown.
                            evt.stop_propagation();
                            value.set(None);
                            open.set(false);
                        },
                        IconClose {}
                    }
                }
                span { class: "filter-chevron", IconChevronDown {} }
            }
            if open() {
                div { class: "filter-backdrop", onclick: move |_| open.set(false) }
                DateFilterPanel { value, on_close: move |()| open.set(false) }
            }
        }
    }
}

/// The open calendar. Picks go into a draft; only Filter applies it.
/// Mounted only while open, so each time it starts from the applied
/// filter and fetches which years have items.
#[component]
fn DateFilterPanel(value: Signal<Option<DateSelection>>, on_close: EventHandler<()>) -> Element {
    let session = use_context::<AuthSession>();
    let mut value = value;
    let today = Local::now().date_naive();
    let this_year = today.year();
    let language = current_language();

    let saved_years = use_resource(move || {
        let session = session.clone();
        async move { session.saved_years().await }
    });
    // The user's years with items, oldest first; None until loaded.
    let years: Option<Vec<i32>> = match &*saved_years.read() {
        Some(Ok(saved)) => Some(local_years(saved, &Local)),
        _ => None,
    };

    let mut draft = use_signal(|| value());
    // Opens where the applied filter was picked: a year on its months, a
    // month on its days.
    let mut stage = use_signal(|| match value() {
        None => Stage::Years,
        Some(DateSelection { month: None, .. }) => Stage::Months,
        Some(_) => Stage::Days,
    });
    // The year grid's page, 0 being the newest; None for the page with
    // the draft's year.
    let mut year_page = use_signal(|| None::<usize>);

    let summary = match draft() {
        Some(selection) => selection.label(language),
        None => t!("date-pick-year"),
    };

    let grid = match (stage(), draft()) {
        (Stage::Days, Some(selection)) if selection.month.is_some() => {
            let month_start = selection.first_day().with_day(1).unwrap();
            let next_month = month_start + Months::new(1);
            let previous_month = month_start - Months::new(1);
            let days_in_month = (next_month - month_start).num_days() as u32;
            // Weeks start on Monday in both UI languages.
            let leading_blanks = month_start.weekday().num_days_from_monday();
            let locale = language.chrono_locale();
            let weekdays: Vec<String> = (0..7)
                .map(|offset| {
                    // 2024-01-01 was a Monday.
                    (NaiveDate::from_ymd_opt(2024, 1, 1).unwrap() + chrono::Days::new(offset))
                        .format_localized("%a", locale)
                        .to_string()
                })
                .collect();
            let go_to = move |month: NaiveDate| {
                EventHandler::new(move |()| {
                    draft.set(Some(DateSelection::month(month.year(), month.month())))
                })
            };

            rsx! {
                CalendarHeader {
                    title: month_year_label(month_start.year(), month_start.month()),
                    on_title: move |()| stage.set(Stage::Months),
                    on_previous: go_to(previous_month),
                    on_next: (next_month <= today).then(|| go_to(next_month)),
                }
                div { class: "date-grid date-grid-days",
                    for weekday in weekdays {
                        span { class: "date-weekday", "{weekday}" }
                    }
                    for _ in 0..leading_blanks {
                        span {}
                    }
                    for day in 1..=days_in_month {
                        {
                            let date = month_start.with_day(day).unwrap();
                            let selected = selection.day == Some(day);
                            rsx! {
                                DateCell {
                                    label: day.to_string(),
                                    selected,
                                    today: date == today,
                                    disabled: date > today,
                                    // Clicking the picked day again unpicks it.
                                    on_pick: move |()| {
                                        draft.set(Some(if selected {
                                            DateSelection::month(date.year(), date.month())
                                        } else {
                                            DateSelection::day(date)
                                        }))
                                    },
                                }
                            }
                        }
                    }
                }
            }
        }
        (Stage::Months | Stage::Days, Some(selection)) => {
            let year = selection.year;
            // The arrows step to the nearest years with items.
            let known = years.as_deref().unwrap_or_default();
            let previous_year = known.iter().rev().copied().find(|&other| other < year);
            let next_year = known.iter().copied().find(|&other| other > year);
            let go_to = move |other: i32| {
                EventHandler::new(move |()| draft.set(Some(DateSelection::year(other))))
            };

            rsx! {
                CalendarHeader {
                    title: year.to_string(),
                    on_title: move |()| {
                        year_page.set(None);
                        stage.set(Stage::Years);
                    },
                    on_previous: previous_year.map(go_to),
                    on_next: next_year.map(go_to),
                }
                div { class: "date-grid",
                    for month in 1..=12u32 {
                        DateCell {
                            label: t!("date-month", month: month),
                            selected: selection.month == Some(month),
                            today: year == this_year && month == today.month(),
                            disabled: year == this_year && month > today.month(),
                            on_pick: move |()| {
                                draft.set(Some(DateSelection::month(year, month)));
                                stage.set(Stage::Days);
                            },
                        }
                    }
                }
            }
        }
        _ => match (&years, &*saved_years.read()) {
            (_, Some(Err(err))) => rsx! {
                p { class: "date-filter-message",
                    {t!("date-years-failed", error: api_error_message(err))}
                }
            },
            (None, _) => rsx! {
                p { class: "date-filter-message", {t!("common-loading")} }
            },
            (Some(years), _) if years.is_empty() => rsx! {
                p { class: "date-filter-message", {t!("date-no-items")} }
            },
            (Some(years), _) => {
                // Newest page first; only the oldest may be short.
                let pages: Vec<&[i32]> = years.rchunks(YEARS_PER_PAGE).collect();
                let page = year_page()
                    .unwrap_or_else(|| {
                        draft().map_or(0, |selection| page_of(years, selection.year))
                    })
                    .min(pages.len() - 1);
                let shown = pages[page].to_vec();
                let (first, last) = (shown[0], shown[shown.len() - 1]);
                let go_to =
                    move |other: usize| EventHandler::new(move |()| year_page.set(Some(other)));

                rsx! {
                    CalendarHeader {
                        title: if first == last { first.to_string() } else { format!("{first} – {last}") },
                        on_previous: (page + 1 < pages.len()).then(|| go_to(page + 1)),
                        on_next: (page > 0).then(|| go_to(page - 1)),
                    }
                    div { class: "date-grid",
                        for year in shown {
                            DateCell {
                                label: year.to_string(),
                                selected: draft().is_some_and(|selection| selection.year == year),
                                today: year == this_year,
                                disabled: false,
                                on_pick: move |()| {
                                    draft.set(Some(DateSelection::year(year)));
                                    stage.set(Stage::Months);
                                },
                            }
                        }
                    }
                }
            }
        },
    };

    rsx! {
        div { class: "filter-panel date-filter-panel",
            {grid}
            div { class: "date-filter-footer",
                span { class: "date-filter-summary", "{summary}" }
                button {
                    class: "date-filter-apply",
                    r#type: "button",
                    disabled: draft().is_none(),
                    onclick: move |_| {
                        value.set(draft());
                        on_close.call(());
                    },
                    {t!("date-apply")}
                }
            }
        }
    }
}

/// `‹  March 2025  ›`. The title goes up a stage when `on_title` is set;
/// an arrow is disabled when its handler isn't (nothing that way).
#[component]
fn CalendarHeader(
    title: String,
    on_title: Option<EventHandler<()>>,
    on_previous: Option<EventHandler<()>>,
    on_next: Option<EventHandler<()>>,
) -> Element {
    rsx! {
        div { class: "date-header",
            button {
                class: "date-nav",
                r#type: "button",
                title: t!("date-previous"),
                aria_label: t!("date-previous"),
                disabled: on_previous.is_none(),
                onclick: move |_| {
                    if let Some(on_previous) = on_previous {
                        on_previous.call(());
                    }
                },
                IconChevronLeft {}
            }
            if let Some(on_title) = on_title {
                button {
                    class: "date-title date-title-button",
                    r#type: "button",
                    onclick: move |_| on_title.call(()),
                    "{title}"
                }
            } else {
                span { class: "date-title", "{title}" }
            }
            button {
                class: "date-nav",
                r#type: "button",
                title: t!("date-next"),
                aria_label: t!("date-next"),
                disabled: on_next.is_none(),
                onclick: move |_| {
                    if let Some(on_next) = on_next {
                        on_next.call(());
                    }
                },
                IconChevronRight {}
            }
        }
    }
}

/// One year, month or day in a calendar grid.
#[component]
fn DateCell(
    label: String,
    selected: bool,
    today: bool,
    disabled: bool,
    on_pick: EventHandler<()>,
) -> Element {
    let mut class = String::from("date-cell");
    if selected {
        class.push_str(" date-cell-selected");
    }
    if today {
        class.push_str(" date-cell-today");
    }

    rsx! {
        button {
            class,
            r#type: "button",
            disabled,
            aria_pressed: if selected { "true" } else { "false" },
            onclick: move |_| on_pick.call(()),
            "{label}"
        }
    }
}

#[cfg(test)]
mod tests {
    use chrono::FixedOffset;

    use super::*;
    use crate::i18n::tests::in_language;

    fn utc(text: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(text)
            .unwrap()
            .with_timezone(&Utc)
    }

    fn saved(first: &str, last: &str) -> SavedYear {
        SavedYear {
            first_saved_at: first.to_string(),
            last_saved_at: last.to_string(),
        }
    }

    #[test]
    fn a_year_month_or_day_covers_its_whole_span() {
        let zone = Utc;
        assert_eq!(
            DateSelection::year(2025).range_in(&zone),
            (utc("2025-01-01T00:00:00Z"), utc("2026-01-01T00:00:00Z"))
        );
        assert_eq!(
            DateSelection::month(2025, 12).range_in(&zone),
            (utc("2025-12-01T00:00:00Z"), utc("2026-01-01T00:00:00Z"))
        );
        // A leap day, and the day after it.
        let leap_day = DateSelection::day(NaiveDate::from_ymd_opt(2024, 2, 29).unwrap());
        assert_eq!(
            leap_day.range_in(&zone),
            (utc("2024-02-29T00:00:00Z"), utc("2024-03-01T00:00:00Z"))
        );
    }

    #[test]
    fn ranges_follow_the_users_clock() {
        // Kyiv in winter: 2025 starts two hours before it does in UTC.
        let kyiv = FixedOffset::east_opt(2 * 3600).unwrap();
        assert_eq!(
            DateSelection::year(2025).range_in(&kyiv),
            (utc("2024-12-31T22:00:00Z"), utc("2025-12-31T22:00:00Z"))
        );
    }

    #[test]
    fn api_range_is_sent_in_utc() {
        let (from, before) = DateSelection::year(2025).api_range();
        for bound in [from, before] {
            assert!(bound.ends_with('Z'), "{bound}");
            assert!(DateTime::parse_from_rfc3339(&bound).is_ok());
        }
    }

    #[test]
    fn labels_name_the_picked_span() {
        in_language(Language::English, || {
            assert_eq!(DateSelection::year(2025).label(Language::English), "2025");
            assert_eq!(
                DateSelection::month(2025, 3).label(Language::English),
                "March 2025"
            );
            let day = DateSelection::day(NaiveDate::from_ymd_opt(2025, 3, 5).unwrap());
            assert_eq!(day.label(Language::English), "5 March 2025");
        });
        in_language(Language::Ukrainian, || {
            // Nominative on its own, genitive after a day number.
            assert_eq!(
                DateSelection::month(2025, 3).label(Language::Ukrainian),
                "березень 2025"
            );
            let day = DateSelection::day(NaiveDate::from_ymd_opt(2025, 3, 5).unwrap());
            assert_eq!(day.label(Language::Ukrainian), "5 березня 2025");
        });
    }

    #[test]
    fn only_years_with_items_are_offered() {
        let saved = [
            saved("2019-05-01T10:00:00Z", "2019-06-01T10:00:00Z"),
            saved("2025-03-01T10:00:00+00:00", "2025-09-01T10:00:00Z"),
        ];
        // No 2020-2024: nothing was saved then.
        assert_eq!(local_years(&saved, &Utc), vec![2019, 2025]);
    }

    #[test]
    fn years_are_the_users_own() {
        // Saved at 23:30 UTC on New Year's Eve: already the next year in
        // Kyiv, still the same one in New York.
        let new_years_eve = [saved("2024-12-31T23:30:00Z", "2024-12-31T23:30:00Z")];
        let kyiv = FixedOffset::east_opt(2 * 3600).unwrap();
        let new_york = FixedOffset::west_opt(5 * 3600).unwrap();
        assert_eq!(local_years(&new_years_eve, &kyiv), vec![2025]);
        assert_eq!(local_years(&new_years_eve, &new_york), vec![2024]);

        // A UTC year whose saves straddle Kyiv's New Year covers both.
        let straddling = [saved("2024-06-01T12:00:00Z", "2024-12-31T23:30:00Z")];
        assert_eq!(local_years(&straddling, &kyiv), vec![2024, 2025]);
    }

    #[test]
    fn unparseable_times_are_skipped() {
        let saved = [
            saved("garbage", "2025-01-01T00:00:00Z"),
            saved("2023-01-01T00:00:00Z", "2023-02-01T00:00:00Z"),
        ];
        assert_eq!(local_years(&saved, &Utc), vec![2023]);
    }

    #[test]
    fn year_pages_count_back_from_the_newest() {
        let years: Vec<i32> = (2000..=2025).collect();
        assert_eq!(page_of(&years, 2025), 0);
        assert_eq!(page_of(&years, 2014), 0);
        assert_eq!(page_of(&years, 2013), 1);
        assert_eq!(page_of(&years, 2000), 2);
        // A year without items: the newest page.
        assert_eq!(page_of(&[2019, 2025], 2021), 0);
    }
}
