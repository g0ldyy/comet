use crate::archive_runtime::{self, CatalogCensus, CatalogEntry};
use crate::inspect::AssetKind;
use crate::materialization::ArchiveExtractionStage;
use crate::resources::Reservation;
use serde::Serialize;
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

pub(crate) const MAX_NESTING_DEPTH: usize = 4;
const MAX_WALL_TIME: Duration = Duration::from_secs(30 * 60);

#[derive(Debug, Eq, PartialEq, Serialize)]
pub(crate) struct NestedMember {
    pub member_id: String,
    pub relative_path: String,
    pub exact_size: u64,
    pub kind: AssetKind,
    pub selected_paths: Vec<String>,
}

struct CumulativeBudget {
    maximum_expanded_bytes: u64,
    entries: usize,
    logical_bytes: u64,
    output_bytes: u64,
    deadline: Instant,
}

impl CumulativeBudget {
    fn new(original_input_bytes: u64) -> Self {
        Self {
            maximum_expanded_bytes: (original_input_bytes * archive_runtime::MAX_COMPRESSION_RATIO)
                .min(archive_runtime::MAX_ARCHIVE_OUTPUT_BYTES),
            entries: 0,
            logical_bytes: 0,
            output_bytes: 0,
            deadline: Instant::now() + MAX_WALL_TIME,
        }
    }

    fn account_census(&mut self, census: &CatalogCensus) -> Result<(), &'static str> {
        self.entries += census.entries;
        self.logical_bytes += census.logical_bytes;
        if self.entries > archive_runtime::MAX_ARCHIVE_ENTRIES
            || self.logical_bytes > self.maximum_expanded_bytes
        {
            return Err("archive_budget_exceeded");
        }
        Ok(())
    }

    fn account_output(&mut self, bytes: u64) -> Result<(), &'static str> {
        self.output_bytes += bytes;
        if self.output_bytes > self.maximum_expanded_bytes {
            return Err("archive_budget_exceeded");
        }
        Ok(())
    }

    fn remaining(&self) -> Result<Duration, &'static str> {
        self.deadline
            .checked_duration_since(Instant::now())
            .filter(|remaining| !remaining.is_zero())
            .ok_or("archive_timed_out")
    }
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn catalog<F>(
    runtime: &archive_runtime::Runtime,
    input: &Path,
    set_identity: &str,
    input_size: u64,
    passphrase: Option<&str>,
    local_data: &Path,
    reservation: &mut Reservation<'_>,
    cancelled: &F,
) -> Result<Vec<NestedMember>, &'static str>
where
    F: Fn() -> bool,
{
    let mut budget = CumulativeBudget::new(input_size);
    let mut members = Vec::new();
    let mut paths = BTreeSet::new();
    catalog_layer(
        runtime,
        input,
        set_identity,
        local_data,
        reservation,
        &mut budget,
        passphrase,
        cancelled,
        &[],
        1,
        &mut paths,
        &mut members,
    )?;
    members.sort_by(|left, right| {
        left.relative_path
            .cmp(&right.relative_path)
            .then_with(|| left.exact_size.cmp(&right.exact_size))
    });
    Ok(members)
}

#[allow(clippy::too_many_arguments)]
fn catalog_layer<F>(
    runtime: &archive_runtime::Runtime,
    input: &Path,
    set_identity: &str,
    local_data: &Path,
    reservation: &mut Reservation<'_>,
    budget: &mut CumulativeBudget,
    passphrase: Option<&str>,
    cancelled: &F,
    parent_paths: &[String],
    depth: usize,
    display_paths: &mut BTreeSet<String>,
    members: &mut Vec<NestedMember>,
) -> Result<(), &'static str>
where
    F: Fn() -> bool,
{
    let catalog_stage = ArchiveExtractionStage::new(local_data)?;
    let census = runtime.catalog_sandboxed(
        input,
        catalog_stage.output(),
        passphrase,
        budget.remaining()?,
        cancelled,
    )?;
    drop(catalog_stage);
    budget.account_census(&census)?;
    for video in census
        .catalog
        .iter()
        .filter(|entry| entry.kind == AssetKind::Video)
    {
        let mut selected_paths = parent_paths.to_vec();
        selected_paths.push(video.relative_path.clone());
        let relative_path = display_path(&selected_paths);
        if !display_paths.insert(relative_path.clone()) {
            return Err("archive_path_conflict");
        }
        members.push(NestedMember {
            member_id: crate::archive_group::member_identity(
                set_identity,
                &relative_path,
                video.exact_size,
            )?,
            relative_path,
            exact_size: video.exact_size,
            kind: AssetKind::Video,
            selected_paths,
        });
    }

    let mut archives = census
        .catalog
        .iter()
        .filter(|entry| entry.kind == AssetKind::Archive)
        .peekable();
    if archives.peek().is_none() {
        return Ok(());
    }
    if depth >= MAX_NESTING_DEPTH {
        return Err("archive_budget_exceeded");
    }
    for archive in archives {
        budget.account_output(archive.exact_size)?;
        reservation.grow(archive.exact_size)?;
        let extraction_stage = ArchiveExtractionStage::new(local_data)?;
        runtime.run_sandboxed(
            input,
            extraction_stage.output(),
            &archive.relative_path,
            archive.exact_size,
            passphrase,
            budget.remaining()?,
            cancelled,
        )?;
        let mut selected_paths = parent_paths.to_vec();
        selected_paths.push(archive.relative_path.clone());
        catalog_layer(
            runtime,
            extraction_stage.output(),
            set_identity,
            local_data,
            reservation,
            budget,
            passphrase,
            cancelled,
            &selected_paths,
            depth + 1,
            display_paths,
            members,
        )?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn extract<F>(
    runtime: &archive_runtime::Runtime,
    input: &Path,
    input_size: u64,
    selected_paths: &[String],
    expected_output_size: u64,
    passphrase: Option<&str>,
    local_data: &Path,
    publication_data: &Path,
    reservation: &mut Reservation<'_>,
    cancelled: &F,
) -> Result<(PathBuf, String, u64, String), &'static str>
where
    F: Fn() -> bool,
{
    if selected_paths.is_empty() || selected_paths.len() > MAX_NESTING_DEPTH {
        return Err("archive_budget_exceeded");
    }
    let mut normalized_paths = Vec::with_capacity(selected_paths.len());
    for path in selected_paths {
        let normalized = crate::archive::normalize_archive_path(path)?;
        if normalized != *path {
            return Err("archive_path_invalid");
        }
        normalized_paths.push(normalized);
    }

    let mut budget = CumulativeBudget::new(input_size);
    let mut current_input = input.to_path_buf();
    let mut intermediate_stages = Vec::with_capacity(normalized_paths.len() - 1);
    for (index, selected_path) in normalized_paths.iter().enumerate() {
        let catalog_stage = ArchiveExtractionStage::new(local_data)?;
        let census = runtime.catalog_sandboxed(
            &current_input,
            catalog_stage.output(),
            passphrase,
            budget.remaining()?,
            cancelled,
        )?;
        drop(catalog_stage);
        budget.account_census(&census)?;
        let entry = exact_entry(&census.catalog, selected_path)?;
        let final_layer = index + 1 == normalized_paths.len();
        let valid_selection = if final_layer {
            entry.kind == AssetKind::Video && entry.exact_size == expected_output_size
        } else {
            entry.kind == AssetKind::Archive
        };
        if !valid_selection {
            return Err("archive_selected_entry_invalid");
        }
        budget.account_output(entry.exact_size)?;
        reservation.grow(entry.exact_size)?;
        let extraction_stage = if final_layer {
            ArchiveExtractionStage::new_with_publication(local_data, publication_data)?
        } else {
            ArchiveExtractionStage::new(local_data)?
        };
        runtime.run_sandboxed(
            &current_input,
            extraction_stage.output(),
            selected_path,
            entry.exact_size,
            passphrase,
            budget.remaining()?,
            cancelled,
        )?;
        if final_layer {
            return extraction_stage.publish();
        }
        current_input = extraction_stage.output().to_path_buf();
        intermediate_stages.push(extraction_stage);
    }
    unreachable!("non-empty archive selection returns from its final layer")
}

fn exact_entry<'a>(
    catalog: &'a [CatalogEntry],
    selected_path: &str,
) -> Result<&'a CatalogEntry, &'static str> {
    catalog
        .binary_search_by(|entry| entry.relative_path.as_str().cmp(selected_path))
        .map(|index| &catalog[index])
        .map_err(|_| "archive_selected_entry_missing")
}

fn display_path(selected_paths: &[String]) -> String {
    selected_paths.join("!/")
}

#[cfg(test)]
mod tests {
    use super::{CumulativeBudget, display_path, exact_entry};
    use crate::archive_runtime::{CatalogCensus, CatalogEntry, MAX_ARCHIVE_ENTRIES};
    use crate::inspect::AssetKind;

    #[test]
    fn cumulative_census_does_not_reset_between_layers() {
        let mut budget = CumulativeBudget::new(1024);
        budget
            .account_census(&CatalogCensus {
                catalog: Vec::new(),
                entries: MAX_ARCHIVE_ENTRIES / 2 + 1,
                logical_bytes: 50_000,
            })
            .expect("first layer");

        assert_eq!(
            budget.account_census(&CatalogCensus {
                catalog: Vec::new(),
                entries: MAX_ARCHIVE_ENTRIES / 2,
                logical_bytes: 1,
            }),
            Err("archive_budget_exceeded")
        );
    }

    #[test]
    fn cumulative_output_uses_the_original_outer_archive_ratio() {
        let mut budget = CumulativeBudget::new(100);
        budget.account_output(6_000).expect("first nested layer");
        assert_eq!(budget.account_output(4_001), Err("archive_budget_exceeded"));
    }

    #[test]
    fn display_path_preserves_the_exact_selected_layer_chain() {
        assert_eq!(
            display_path(&["payload.tar.gz".to_owned(), "Movie.2026.mkv".to_owned()]),
            "payload.tar.gz!/Movie.2026.mkv"
        );
    }

    #[test]
    fn exact_selection_distinguishes_case_sensitive_archive_paths() {
        let catalog = [
            CatalogEntry {
                relative_path: "Movie.mkv".to_owned(),
                exact_size: 1,
                kind: AssetKind::Video,
            },
            CatalogEntry {
                relative_path: "movie.mkv".to_owned(),
                exact_size: 2,
                kind: AssetKind::Video,
            },
        ];

        assert_eq!(
            exact_entry(&catalog, "Movie.mkv")
                .expect("select first exact path")
                .exact_size,
            1
        );
        assert_eq!(
            exact_entry(&catalog, "movie.mkv")
                .expect("select second exact path")
                .exact_size,
            2
        );
        assert_eq!(
            exact_entry(&catalog, "MOVIE.mkv"),
            Err("archive_selected_entry_missing")
        );
    }
}
