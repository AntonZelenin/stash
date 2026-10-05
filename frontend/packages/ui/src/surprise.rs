//! "Surprise me": random items, open one at a time, and the trail of those
//! shown so far. Previous steps back along the trail; Next steps forward
//! along it and, from its end, asks the server for another random item.

use api::ListedItem;

use crate::viewer_nav::Step;

/// The items "Surprise me" has shown, oldest first, and which is open.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct SurpriseTrail {
    items: Vec<ListedItem>,
    index: usize,
    /// How the open item was reached, if by stepping (see `ViewerNav`).
    pub arrived_by: Option<Step>,
}

impl SurpriseTrail {
    pub(crate) fn new(first: ListedItem) -> Self {
        Self {
            items: vec![first],
            index: 0,
            arrived_by: None,
        }
    }

    pub(crate) fn current(&self) -> &ListedItem {
        &self.items[self.index]
    }

    pub(crate) fn has_previous(&self) -> bool {
        self.index > 0
    }

    /// Whether the open item is the last one shown, so Next needs a new one.
    pub(crate) fn at_end(&self) -> bool {
        self.index + 1 == self.items.len()
    }

    /// Opens the item `step` away along the trail; false (and nothing
    /// changes) past either end.
    pub(crate) fn step(&mut self, step: Step) -> bool {
        let index = match step {
            Step::Previous => self.index.checked_sub(1),
            Step::Next => Some(self.index + 1).filter(|&index| index < self.items.len()),
        };
        let Some(index) = index else {
            return false;
        };
        self.index = index;
        self.arrived_by = Some(step);
        true
    }

    /// Adds a newly picked item to the end of the trail, and opens it.
    pub(crate) fn push(&mut self, item: ListedItem) {
        self.items.push(item);
        self.index = self.items.len() - 1;
        self.arrived_by = Some(Step::Next);
    }

    /// Replaces the trail's copy of `item` (as saved after a change), so
    /// stepping back to it shows it as it is now.
    pub(crate) fn update(&mut self, item: ListedItem) {
        for entry in &mut self.items {
            if entry.id == item.id {
                *entry = item.clone();
            }
        }
    }

    /// Drops the `deleted` items. None if the open one was among them (the
    /// view closes, as when deleting any open item).
    pub(crate) fn without(mut self, deleted: &[String]) -> Option<Self> {
        if deleted.contains(&self.current().id) {
            return None;
        }
        let current = self.current().id.clone();
        self.items.retain(|item| !deleted.contains(&item.id));
        self.index = self.items.iter().position(|item| item.id == current)?;
        Some(self)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn item(id: &str) -> ListedItem {
        ListedItem {
            id: id.to_string(),
            r#type: "text".to_string(),
            status: "ready".to_string(),
            created_at: "2026-09-24T12:21:14Z".to_string(),
            text: Some(id.to_string()),
            search_note: None,
            download_url: None,
            thumbnail_url: None,
            file: None,
            tags: Vec::new(),
            collections: Vec::new(),
            is_favorite: false,
        }
    }

    fn trail(ids: &[&str]) -> SurpriseTrail {
        let mut trail = SurpriseTrail::new(item(ids[0]));
        for id in &ids[1..] {
            trail.push(item(id));
        }
        trail
    }

    #[test]
    fn a_new_trail_has_nothing_to_step_to() {
        let mut trail = SurpriseTrail::new(item("a"));
        assert!(!trail.has_previous() && trail.at_end());
        assert!(!trail.step(Step::Previous) && !trail.step(Step::Next));
        assert_eq!(trail.current().id, "a");
        assert_eq!(trail.arrived_by, None);
    }

    #[test]
    fn picked_items_open_at_the_end_of_the_trail() {
        let trail = trail(&["a", "b", "c"]);
        assert_eq!(trail.current().id, "c");
        assert!(trail.has_previous() && trail.at_end());
        assert_eq!(trail.arrived_by, Some(Step::Next));
    }

    #[test]
    fn stepping_walks_back_and_forth_along_the_trail() {
        let mut trail = trail(&["a", "b", "c"]);
        assert!(trail.step(Step::Previous));
        assert!(trail.step(Step::Previous));
        assert_eq!(trail.current().id, "a");
        assert_eq!(trail.arrived_by, Some(Step::Previous));
        assert!(!trail.step(Step::Previous));
        assert!(trail.step(Step::Next));
        assert_eq!(trail.current().id, "b");
        assert!(!trail.at_end());
        assert!(trail.step(Step::Next));
        assert!(trail.at_end());
        // From the end, Next needs a new item rather than stepping.
        assert!(!trail.step(Step::Next));
        assert_eq!(trail.current().id, "c");
    }

    #[test]
    fn updates_replace_the_trails_copy() {
        let mut trail = trail(&["a", "b"]);
        trail.update(ListedItem {
            is_favorite: true,
            ..item("a")
        });
        trail.step(Step::Previous);
        assert!(trail.current().is_favorite);
    }

    #[test]
    fn deleting_the_open_item_closes_the_trail() {
        let trail = trail(&["a", "b"]);
        assert_eq!(trail.without(&["b".to_string()]), None);
    }

    #[test]
    fn deleted_items_drop_out_of_the_trail() {
        let mut trail = trail(&["a", "b", "c"]);
        trail.step(Step::Previous);
        let mut trail = trail.without(&["a".to_string()]).unwrap();
        assert_eq!(trail.current().id, "b");
        assert!(!trail.has_previous());
        assert!(trail.step(Step::Next));
        assert_eq!(trail.current().id, "c");
    }
}
