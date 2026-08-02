use crate::archive::{VolumeScheme, classify_volume_name, normalize_archive_path};
use crate::nzb;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;

pub const MAX_STRUCTURAL_END_BYTES: usize = 2 * 1024 * 1024;
const MAX_CATALOG_FILES: usize = nzb::MAX_FILES;
const MAX_CATALOG_POSTINGS: usize = nzb::MAX_SEGMENTS;
const MAX_POSTINGS_PER_FILE: usize = nzb::MAX_SEGMENTS;
const MAX_TEXT_BYTES: usize = 16 * 1024;
const MAX_ARTICLE_BYTES: u64 = crate::limits::MAX_DECLARED_POSTING_BYTES;
const VIDEO_EXTENSIONS: &[&str] = &[
    "3g2", "3gp", "amv", "asf", "avi", "drc", "f4a", "f4b", "f4p", "f4v", "flv", "gif", "gifv",
    "m2ts", "m2v", "m4p", "m4v", "mkv", "mng", "mov", "mp2", "mp4", "mpe", "mpeg", "mpg", "mpv",
    "mts", "mxf", "nsv", "ogg", "ogv", "qt", "rm", "rmvb", "roq", "svi", "ts", "webm", "wmv",
    "yuv",
];

fn ends_with_ascii_ci(path: &str, suffix: &str) -> bool {
    if path.len() < suffix.len() {
        return false;
    }
    let start = path.len() - suffix.len();
    path.is_char_boundary(start) && path[start..].eq_ignore_ascii_case(suffix)
}

fn is_video_path(path: &str) -> bool {
    path.rsplit_once('.').is_some_and(|(_, extension)| {
        VIDEO_EXTENSIONS
            .iter()
            .any(|candidate| extension.eq_ignore_ascii_case(candidate))
    })
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AssetKind {
    Video,
    Archive,
    Split,
    LogicalSplit,
    LogicalArchive,
    Par2,
    Par2Source,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct CatalogAsset {
    pub asset_id: String,
    pub file_index: usize,
    pub relative_path: String,
    pub declared_bytes: u64,
    pub kind: AssetKind,
}

fn valid_manifest_text(value: &str) -> bool {
    value.len() <= MAX_TEXT_BYTES && !value.bytes().any(|byte| byte.is_ascii_control())
}

fn valid_manifest_message_id(value: &str) -> bool {
    crate::nntp::canonical_message_id(value) == Ok(value)
}

fn valid_manifest_metadata(metadata: &std::collections::BTreeMap<String, String>) -> bool {
    metadata.iter().all(|(key, value)| {
        nzb::known_metadata(key) && !value.is_empty() && valid_manifest_text(value)
    })
}

fn declared_primary_bytes(file: &nzb::File) -> Result<u64, &'static str> {
    if file.postings.is_empty() || file.postings.len() > MAX_POSTINGS_PER_FILE {
        return Err("asset_catalog_invalid");
    }
    let mut previous = 0_u64;
    let mut declared = 0_u64;
    for posting in &file.postings {
        if posting.number == 0
            || posting.number > MAX_POSTINGS_PER_FILE as u64
            || posting.number < previous
            || posting.bytes == 0
            || posting.bytes > MAX_ARTICLE_BYTES
            || !valid_manifest_message_id(&posting.message_id)
        {
            return Err("asset_catalog_invalid");
        }
        if posting.number != previous {
            if posting.number != previous + 1 {
                return Err("asset_catalog_invalid");
            }
            declared = declared
                .checked_add(posting.bytes)
                .filter(|value| *value <= crate::limits::MAX_LOGICAL_BYTES)
                .ok_or("asset_catalog_invalid")?;
            previous = posting.number;
        }
    }
    Ok(declared)
}

fn validate_manifest_file(file: &nzb::File) -> Result<u64, &'static str> {
    if file
        .subject
        .as_deref()
        .is_some_and(|value| !valid_manifest_text(value))
        || file
            .poster
            .as_deref()
            .is_some_and(|value| !valid_manifest_text(value))
        || file.groups.iter().any(|value| !valid_manifest_text(value))
        || !file
            .groups
            .windows(2)
            .all(|values| values[0].as_bytes() < values[1].as_bytes())
        || file.metadata.iter().any(|(key, value)| {
            !nzb::known_metadata(key) || !valid_manifest_text(value) || value.is_empty()
        })
    {
        return Err("asset_catalog_invalid");
    }
    declared_primary_bytes(file)
}

fn quoted_subject_candidates(subject: &str) -> Vec<&str> {
    let mut candidates = Vec::new();
    let mut opening = None;
    for (offset, character) in subject.char_indices() {
        if !matches!(character, '"' | '\'') {
            continue;
        }
        match opening {
            Some((start, quote)) if quote == character => {
                if start < offset {
                    candidates.push(&subject[start..offset]);
                }
                opening = None;
            }
            None => opening = Some((offset + character.len_utf8(), character)),
            Some(_) => {}
        }
    }
    candidates
}

fn subject_candidates(subject: &str) -> Vec<&str> {
    let quoted = quoted_subject_candidates(subject);
    if quoted.is_empty() {
        subject.split_ascii_whitespace().collect()
    } else {
        quoted
    }
}

pub(crate) fn classify_path(path: &str) -> Option<AssetKind> {
    if ends_with_ascii_ci(path, "par2") {
        return Some(AssetKind::Par2);
    }
    if let Some(volume) = classify_volume_name(path) {
        return Some(match volume.scheme {
            VolumeScheme::NumericSplit => AssetKind::Split,
            VolumeScheme::RarPart
            | VolumeScheme::RarLegacy
            | VolumeScheme::SevenZipSplit
            | VolumeScheme::ZipSplit => AssetKind::Archive,
        });
    }
    if ends_with_ascii_ci(path, ".7z")
        || ends_with_ascii_ci(path, ".zip")
        || ends_with_ascii_ci(path, ".tar")
        || ends_with_ascii_ci(path, ".tar.gz")
        || ends_with_ascii_ci(path, ".tgz")
    {
        return Some(AssetKind::Archive);
    }
    is_video_path(path).then_some(AssetKind::Video)
}

fn catalog_paths(file: &nzb::File) -> BTreeSet<(String, AssetKind)> {
    let raw_candidates = file.metadata.get("name").map_or_else(
        || {
            file.subject
                .as_deref()
                .map(subject_candidates)
                .unwrap_or_default()
        },
        |name| vec![name.as_str()],
    );
    raw_candidates
        .into_iter()
        .filter_map(|candidate| {
            let candidate = candidate.trim_matches(|character| matches!(character, '"' | '\''));
            let path = normalize_archive_path(candidate).ok()?;
            let kind = classify_path(&path)?;
            Some((path, kind))
        })
        .collect()
}

fn catalog_is(cataloged: &BTreeSet<(String, AssetKind)>, kind: AssetKind) -> bool {
    cataloged.len() == 1
        && cataloged
            .iter()
            .next()
            .is_some_and(|(_, candidate)| *candidate == kind)
}

fn artifact_digest(value: &str) -> Result<[u8; 32], &'static str> {
    if value.len() != 64 {
        return Err("asset_catalog_invalid");
    }
    let mut digest = [0_u8; 32];
    for (index, pair) in value.as_bytes().chunks_exact(2).enumerate() {
        let hex = |byte| match byte {
            b'0'..=b'9' => Some(byte - b'0'),
            b'a'..=b'f' => Some(byte - b'a' + 10),
            _ => None,
        };
        digest[index] = hex(pair[0])
            .and_then(|high| hex(pair[1]).map(|low| (high << 4) | low))
            .ok_or("asset_catalog_invalid")?;
    }
    Ok(digest)
}

fn asset_id(artifact: [u8; 32], file_index: usize, relative_path: &str) -> String {
    let path = relative_path.as_bytes();
    let mut digest = Sha256::new();
    digest.update(b"comet-nzb-asset-v1\0");
    digest.update(artifact);
    digest.update((file_index as u32).to_be_bytes());
    digest.update((path.len() as u32).to_be_bytes());
    digest.update(path);
    format!("{:x}", digest.finalize())
}

pub fn catalog_manifest(
    artifact_sha256: &str,
    expected_manifest_identity: &str,
    metadata: &std::collections::BTreeMap<String, String>,
    files: &[nzb::File],
    selection_hint: Option<(&str, u64)>,
) -> Result<Vec<CatalogAsset>, &'static str> {
    if files.is_empty()
        || files.len() > MAX_CATALOG_FILES
        || !valid_manifest_metadata(metadata)
        || expected_manifest_identity.len() != 68
        || nzb::manifest_identity(metadata, files) != expected_manifest_identity
    {
        return Err("asset_catalog_invalid");
    }
    let artifact = artifact_digest(artifact_sha256)?;
    files.iter().try_fold(0usize, |total, file| {
        total
            .checked_add(file.postings.len())
            .filter(|value| *value <= MAX_CATALOG_POSTINGS)
            .ok_or("asset_catalog_invalid")
    })?;
    let selection_hint = match selection_hint {
        Some((hinted_path, hinted_size)) => {
            if normalize_archive_path(hinted_path).as_deref() != Ok(hinted_path)
                || !(1..=crate::limits::MAX_LOGICAL_BYTES).contains(&hinted_size)
            {
                return Err("asset_catalog_invalid");
            }
            Some((hinted_path, hinted_size))
        }
        None => None,
    };
    let mut cataloged_files = Vec::with_capacity(files.len());
    for file in files {
        cataloged_files.push((validate_manifest_file(file)?, catalog_paths(file)));
    }
    let has_par2 = cataloged_files
        .iter()
        .any(|(_, cataloged)| catalog_is(cataloged, AssetKind::Par2));
    let metadata_video_path = metadata.get("title").and_then(|title| {
        let path = normalize_archive_path(title).ok()?;
        (classify_path(&path) == Some(AssetKind::Video)).then_some(path)
    });
    let metadata_video_file = metadata_video_path.as_ref().and_then(|_| {
        (selection_hint.is_none()
            && cataloged_files
                .iter()
                .all(|(_, cataloged)| cataloged.is_empty()))
        .then(|| {
            cataloged_files
                .iter()
                .enumerate()
                .max_by_key(|(_, (declared_bytes, _))| *declared_bytes)
                .map(|(file_index, _)| file_index)
                .expect("non-empty validated NZB")
        })
    });
    let source_file_count = cataloged_files
        .iter()
        .filter(|(_, cataloged)| !catalog_is(cataloged, AssetKind::Par2))
        .count();
    let logical_split_path = selection_hint
        .filter(|_| {
            (2..=crate::raw_composite::MAX_COMPOSITE_PARTS).contains(&source_file_count)
                && cataloged_files.iter().all(|(_, cataloged)| {
                    cataloged.is_empty() || catalog_is(cataloged, AssetKind::Par2)
                })
        })
        .map(|(path, _)| path);
    let logical_archive_path = selection_hint
        .filter(|_| {
            source_file_count > 1
                && cataloged_files.iter().all(|(_, cataloged)| {
                    catalog_is(cataloged, AssetKind::Archive)
                        || catalog_is(cataloged, AssetKind::Par2)
                })
        })
        .map(|(path, _)| path);
    let mut assets = Vec::new();
    let mut paths = BTreeSet::new();
    let mut logical_part = 0;
    for (file_index, (declared_bytes, cataloged)) in cataloged_files.into_iter().enumerate() {
        let (relative_path, kind, declared_bytes) = match cataloged.len() {
            1 if logical_archive_path.is_some() && catalog_is(&cataloged, AssetKind::Archive) => {
                let (path, _) = cataloged.into_iter().next().expect("single catalog path");
                (
                    normalize_archive_path(&format!(
                        "{}/archive/{}",
                        logical_archive_path.expect("checked logical archive path"),
                        path
                    ))?,
                    AssetKind::LogicalArchive,
                    declared_bytes,
                )
            }
            1 => {
                let (path, kind) = cataloged.into_iter().next().expect("single catalog path");
                (path, kind, declared_bytes)
            }
            0 if metadata_video_file == Some(file_index) => (
                metadata_video_path
                    .clone()
                    .expect("metadata video path selected"),
                AssetKind::Video,
                declared_bytes,
            ),
            0 if metadata_video_file.is_some() => (
                format!("par2-candidate/{file_index:04}.par2"),
                AssetKind::Par2,
                declared_bytes,
            ),
            0 if logical_split_path.is_some() => {
                logical_part += 1;
                (
                    format!(
                        "{}/part.{logical_part:03}",
                        logical_split_path.expect("checked logical split path"),
                    ),
                    AssetKind::LogicalSplit,
                    declared_bytes,
                )
            }
            0 if source_file_count == 1 => {
                let Some((hinted_path, hinted_size)) = selection_hint else {
                    continue;
                };
                (hinted_path.to_owned(), AssetKind::Video, hinted_size)
            }
            0 if has_par2 => (
                format!("par2-source/{file_index:04}"),
                AssetKind::Par2Source,
                declared_bytes,
            ),
            _ => continue,
        };
        if !paths.insert(relative_path.to_lowercase()) {
            return Err("asset_catalog_path_conflict");
        }
        assets.push(CatalogAsset {
            asset_id: asset_id(artifact, file_index, &relative_path),
            file_index,
            relative_path,
            declared_bytes,
            kind,
        });
    }
    Ok(assets)
}

const EBML_ID: u64 = 0x1a45dfa3;
const EBML_SIGNATURE: &[u8; 4] = b"\x1a\x45\xdf\xa3";
const EBML_DOCTYPE_ID: u64 = 0x4282;
const EBML_SEGMENT_ID: u64 = 0x18538067;
const EBML_INFO_ID: u64 = 0x1549a966;
const EBML_TIMECODE_SCALE_ID: u64 = 0x2ad7b1;
const EBML_DURATION_ID: u64 = 0x4489;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ContainerKind {
    Matroska,
    WebM,
    Mp4,
    QuickTime,
}

impl ContainerKind {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Matroska => "matroska",
            Self::WebM => "webm",
            Self::Mp4 => "mp4",
            Self::QuickTime => "quicktime",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ContainerEvidence {
    pub kind: ContainerKind,
    pub duration_millis: Option<u64>,
    pub inspected_head_bytes: usize,
    pub inspected_tail_bytes: usize,
}

pub fn probe_container(head: &[u8], tail: &[u8]) -> Result<ContainerEvidence, &'static str> {
    if head.is_empty()
        || head.len() > MAX_STRUCTURAL_END_BYTES
        || tail.len() > MAX_STRUCTURAL_END_BYTES
    {
        return Err("container_probe_budget");
    }
    let mut evidence = if head.starts_with(EBML_SIGNATURE) {
        probe_ebml(head)?
    } else if head.get(4..8) == Some(b"ftyp") {
        probe_iso(head, tail)?
    } else {
        return Err("container_signature_mismatch");
    };
    evidence.inspected_head_bytes = head.len();
    evidence.inspected_tail_bytes = tail.len();
    Ok(evidence)
}

#[derive(Clone, Copy)]
struct IsoBox {
    kind: [u8; 4],
    content_start: usize,
    end: usize,
}

fn iso_box(data: &[u8], start: usize) -> Result<IsoBox, &'static str> {
    let header = data
        .get(start..start.checked_add(8).ok_or("container_structure_invalid")?)
        .ok_or("container_probe_incomplete")?;
    let size32 = u32::from_be_bytes(header[..4].try_into().expect("four-byte ISO size"));
    let kind = header[4..8].try_into().expect("four-byte ISO kind");
    let (header_bytes, size) = match size32 {
        0 => (
            8usize,
            u64::try_from(data.len().saturating_sub(start))
                .map_err(|_| "container_structure_invalid")?,
        ),
        1 => {
            let extended = data
                .get(
                    start.checked_add(8).ok_or("container_structure_invalid")?
                        ..start.checked_add(16).ok_or("container_structure_invalid")?,
                )
                .ok_or("container_probe_incomplete")?;
            (
                16,
                u64::from_be_bytes(extended.try_into().expect("eight-byte ISO size")),
            )
        }
        value => (8, u64::from(value)),
    };
    if size < header_bytes as u64 {
        return Err("container_structure_invalid");
    }
    let end = u64::try_from(start)
        .ok()
        .and_then(|start| start.checked_add(size))
        .and_then(|end| usize::try_from(end).ok())
        .ok_or("container_structure_invalid")?;
    if end > data.len() {
        return Err("container_probe_incomplete");
    }
    Ok(IsoBox {
        kind,
        content_start: start
            .checked_add(header_bytes)
            .ok_or("container_structure_invalid")?,
        end,
    })
}

fn probe_iso(head: &[u8], tail: &[u8]) -> Result<ContainerEvidence, &'static str> {
    let ftyp = iso_box(head, 0)?;
    if &ftyp.kind != b"ftyp" || ftyp.end - ftyp.content_start < 8 {
        return Err("container_structure_invalid");
    }
    let brands = &head[ftyp.content_start..ftyp.end];
    if !brands.len().is_multiple_of(4) {
        return Err("container_structure_invalid");
    }
    let major_brand = &brands[..4];
    let compatible_brands = &brands[8..];
    let mut is_quicktime = false;
    for brand in std::iter::once(major_brand).chain(compatible_brands.chunks_exact(4)) {
        if brand == b"qt  " {
            is_quicktime = true;
        }
    }
    let kind = if is_quicktime {
        ContainerKind::QuickTime
    } else {
        ContainerKind::Mp4
    };
    let duration_millis = find_head_moov(head, ftyp.end)
        .or_else(|| find_tail_moov(tail))
        .transpose()?
        .flatten();
    Ok(ContainerEvidence {
        kind,
        duration_millis,
        inspected_head_bytes: 0,
        inspected_tail_bytes: 0,
    })
}

fn find_head_moov(data: &[u8], mut offset: usize) -> Option<Result<Option<u64>, &'static str>> {
    while offset < data.len() {
        let box_ = match iso_box(data, offset) {
            Ok(box_) => box_,
            Err("container_probe_incomplete") => return None,
            Err(code) => return Some(Err(code)),
        };
        if &box_.kind == b"moov" {
            return Some(parse_moov(data, box_));
        }
        if box_.end <= offset {
            return Some(Err("container_structure_invalid"));
        }
        offset = box_.end;
    }
    None
}

fn find_tail_moov(data: &[u8]) -> Option<Result<Option<u64>, &'static str>> {
    for (kind_start, window) in data.windows(4).enumerate().skip(4) {
        if window != b"moov" {
            continue;
        }
        let start = kind_start - 4;
        let box_ = match iso_box(data, start) {
            Ok(box_) if &box_.kind == b"moov" => box_,
            _ => continue,
        };
        let mut suffix = box_.end;
        let mut valid_suffix = true;
        while suffix < data.len() {
            match iso_box(data, suffix) {
                Ok(next) if next.end > suffix => suffix = next.end,
                _ => {
                    valid_suffix = false;
                    break;
                }
            }
        }
        if valid_suffix && suffix == data.len() {
            return Some(parse_moov(data, box_));
        }
    }
    None
}

fn parse_moov(data: &[u8], moov: IsoBox) -> Result<Option<u64>, &'static str> {
    let mut offset = moov.content_start;
    while offset < moov.end {
        let child = iso_box(data, offset)?;
        if child.end > moov.end || child.end <= offset {
            return Err("container_structure_invalid");
        }
        if &child.kind == b"mvhd" {
            return parse_mvhd(&data[child.content_start..child.end]);
        }
        offset = child.end;
    }
    Err("container_structure_invalid")
}

fn parse_mvhd(payload: &[u8]) -> Result<Option<u64>, &'static str> {
    let version = *payload.first().ok_or("container_structure_invalid")?;
    let (timescale, duration) = match version {
        0 if payload.len() >= 20 => (
            u64::from(u32::from_be_bytes(
                payload[12..16].try_into().expect("mvhd v0 timescale"),
            )),
            u64::from(u32::from_be_bytes(
                payload[16..20].try_into().expect("mvhd v0 duration"),
            )),
        ),
        1 if payload.len() >= 32 => (
            u64::from(u32::from_be_bytes(
                payload[20..24].try_into().expect("mvhd v1 timescale"),
            )),
            u64::from_be_bytes(payload[24..32].try_into().expect("mvhd v1 duration")),
        ),
        _ => return Err("container_structure_invalid"),
    };
    if timescale == 0 {
        return Err("container_structure_invalid");
    }
    if duration == 0 {
        return Ok(None);
    }
    u128::from(duration)
        .checked_mul(1000)
        .map(|millis| millis / u128::from(timescale))
        .and_then(|millis| u64::try_from(millis).ok())
        .map(Some)
        .ok_or("container_structure_invalid")
}

#[derive(Clone, Copy)]
struct EbmlElement {
    id: u64,
    content_start: usize,
    end: Option<usize>,
}

fn ebml_element(data: &[u8], offset: usize) -> Result<EbmlElement, &'static str> {
    let (id, id_bytes) = ebml_id(data, offset)?;
    let size_offset = offset
        .checked_add(id_bytes)
        .ok_or("container_structure_invalid")?;
    let (size, size_bytes) = ebml_size(data, size_offset)?;
    let content_start = size_offset
        .checked_add(size_bytes)
        .ok_or("container_structure_invalid")?;
    let end = size
        .and_then(|size| u64::try_from(content_start).ok()?.checked_add(size))
        .and_then(|end| usize::try_from(end).ok())
        .filter(|end| *end <= data.len());
    Ok(EbmlElement {
        id,
        content_start,
        end,
    })
}

fn ebml_id(data: &[u8], offset: usize) -> Result<(u64, usize), &'static str> {
    let first = *data.get(offset).ok_or("container_probe_incomplete")?;
    let length = first.leading_zeros() as usize + 1;
    if first == 0 || length > 4 {
        return Err("container_structure_invalid");
    }
    let bytes = data
        .get(
            offset
                ..offset
                    .checked_add(length)
                    .ok_or("container_structure_invalid")?,
        )
        .ok_or("container_probe_incomplete")?;
    Ok((
        bytes
            .iter()
            .fold(0_u64, |value, byte| (value << 8) | u64::from(*byte)),
        length,
    ))
}

fn ebml_size(data: &[u8], offset: usize) -> Result<(Option<u64>, usize), &'static str> {
    let first = *data.get(offset).ok_or("container_probe_incomplete")?;
    let length = first.leading_zeros() as usize + 1;
    if first == 0 || length > 8 {
        return Err("container_structure_invalid");
    }
    let bytes = data
        .get(
            offset
                ..offset
                    .checked_add(length)
                    .ok_or("container_structure_invalid")?,
        )
        .ok_or("container_probe_incomplete")?;
    let marker = 1_u8 << (8 - length);
    let mut value = u64::from(first & !marker);
    for byte in &bytes[1..] {
        value = (value << 8) | u64::from(*byte);
    }
    let unknown = value == (1_u64 << (7 * length)) - 1;
    Ok(((!unknown).then_some(value), length))
}

fn probe_ebml(head: &[u8]) -> Result<ContainerEvidence, &'static str> {
    let ebml = ebml_element(head, 0)?;
    if ebml.id != EBML_ID {
        return Err("container_signature_mismatch");
    }
    let ebml_end = ebml.end.ok_or("container_probe_incomplete")?;
    let mut doctype = None;
    let mut offset = ebml.content_start;
    while offset < ebml_end {
        let element = ebml_element(head, offset)?;
        let end = element.end.ok_or("container_probe_incomplete")?;
        if end > ebml_end || end <= offset {
            return Err("container_structure_invalid");
        }
        if element.id == EBML_DOCTYPE_ID {
            doctype = Some(
                std::str::from_utf8(&head[element.content_start..end])
                    .map_err(|_| "container_structure_invalid")?,
            );
        }
        offset = end;
    }
    let kind = match doctype {
        Some("matroska") => ContainerKind::Matroska,
        Some("webm") => ContainerKind::WebM,
        _ => return Err("container_structure_invalid"),
    };
    let segment = ebml_element(head, ebml_end)?;
    if segment.id != EBML_SEGMENT_ID {
        return Err("container_structure_invalid");
    }
    let segment_end = segment.end.unwrap_or(head.len()).min(head.len());
    let mut offset = segment.content_start;
    while offset < segment_end {
        let element = match ebml_element(head, offset) {
            Ok(element) => element,
            Err("container_probe_incomplete") => break,
            Err(code) => return Err(code),
        };
        let Some(end) = element.end.filter(|end| *end <= segment_end) else {
            break;
        };
        if end > segment_end || end <= offset {
            return Err("container_structure_invalid");
        }
        if element.id == EBML_INFO_ID {
            return Ok(ContainerEvidence {
                kind,
                duration_millis: parse_ebml_info(&head[element.content_start..end])?,
                inspected_head_bytes: 0,
                inspected_tail_bytes: 0,
            });
        }
        offset = end;
    }
    Ok(ContainerEvidence {
        kind,
        duration_millis: None,
        inspected_head_bytes: 0,
        inspected_tail_bytes: 0,
    })
}

fn parse_ebml_info(data: &[u8]) -> Result<Option<u64>, &'static str> {
    let mut scale = 1_000_000_u64;
    let mut duration = None;
    let mut offset = 0usize;
    while offset < data.len() {
        let element = ebml_element(data, offset)?;
        let end = element.end.ok_or("container_probe_incomplete")?;
        if end <= offset {
            return Err("container_structure_invalid");
        }
        let payload = &data[element.content_start..end];
        match element.id {
            EBML_TIMECODE_SCALE_ID => {
                scale = ebml_uint(payload)?;
                if scale == 0 {
                    return Err("container_structure_invalid");
                }
            }
            EBML_DURATION_ID => duration = Some(ebml_float(payload)?),
            _ => {}
        }
        offset = end;
    }
    let Some(duration) = duration else {
        return Ok(None);
    };
    let millis = duration * scale as f64 / 1_000_000_f64;
    if !millis.is_finite() || millis < 0.0 || millis > u64::MAX as f64 {
        return Err("container_structure_invalid");
    }
    Ok(Some(millis.round() as u64))
}

fn ebml_uint(payload: &[u8]) -> Result<u64, &'static str> {
    if payload.is_empty() || payload.len() > 8 {
        return Err("container_structure_invalid");
    }
    Ok(payload
        .iter()
        .fold(0_u64, |value, byte| (value << 8) | u64::from(*byte)))
}

fn ebml_float(payload: &[u8]) -> Result<f64, &'static str> {
    let value = match payload.len() {
        4 => f64::from(f32::from_bits(u32::from_be_bytes(
            payload.try_into().expect("four-byte EBML float"),
        ))),
        8 => f64::from_bits(u64::from_be_bytes(
            payload.try_into().expect("eight-byte EBML float"),
        )),
        _ => return Err("container_structure_invalid"),
    };
    value
        .is_finite()
        .then_some(value)
        .ok_or("container_structure_invalid")
}

#[cfg(test)]
mod tests {
    use super::{
        AssetKind, ContainerKind, MAX_ARTICLE_BYTES, catalog_manifest, classify_path,
        declared_primary_bytes, probe_container,
    };
    use crate::nzb;

    #[test]
    fn classifies_paths_case_insensitively_without_allocating() {
        assert_eq!(classify_path("Show.S01E01.MKV"), Some(AssetKind::Video));
        assert_eq!(classify_path("Show.PAR2"), Some(AssetKind::Par2));
        assert_eq!(classify_path("data.ZIP"), Some(AssetKind::Archive));
        assert_eq!(classify_path("data.RAR"), Some(AssetKind::Archive));
        assert_eq!(classify_path("movie.TAR.GZ"), Some(AssetKind::Archive));
        assert_eq!(classify_path("capture.M2TS"), Some(AssetKind::Video));
        assert_eq!(classify_path("legacy.AVI"), Some(AssetKind::Video));
        assert_eq!(classify_path("notes.txt"), None);
        assert_eq!(classify_path("opaque"), None);
    }

    fn iso_box(kind: &[u8; 4], payload: &[u8]) -> Vec<u8> {
        let mut result = Vec::with_capacity(payload.len() + 8);
        result.extend_from_slice(
            &u32::try_from(payload.len() + 8)
                .expect("bounded ISO fixture")
                .to_be_bytes(),
        );
        result.extend_from_slice(kind);
        result.extend_from_slice(payload);
        result
    }

    fn mvhd(timescale: u32, duration: u32) -> Vec<u8> {
        let mut payload = vec![0_u8; 12];
        payload.extend_from_slice(&timescale.to_be_bytes());
        payload.extend_from_slice(&duration.to_be_bytes());
        iso_box(b"mvhd", &payload)
    }

    fn ebml_element(id: &[u8], payload: &[u8]) -> Vec<u8> {
        assert!(payload.len() < 127);
        let mut result = id.to_vec();
        result.push(0x80 | payload.len() as u8);
        result.extend_from_slice(payload);
        result
    }

    fn ebml_container(doctype: &[u8], scale: &[u8], duration: f64) -> Vec<u8> {
        let doctype = ebml_element(&[0x42, 0x82], doctype);
        let header = ebml_element(&[0x1a, 0x45, 0xdf, 0xa3], &doctype);
        let scale = ebml_element(&[0x2a, 0xd7, 0xb1], scale);
        let duration = ebml_element(&[0x44, 0x89], &duration.to_be_bytes());
        let mut info_payload = scale;
        info_payload.extend_from_slice(&duration);
        let info = ebml_element(&[0x15, 0x49, 0xa9, 0x66], &info_payload);
        let mut head = header;
        head.extend_from_slice(&[0x18, 0x53, 0x80, 0x67, 0xff]);
        head.extend_from_slice(&info);
        head
    }

    fn catalog(document: &[u8]) -> Vec<super::CatalogAsset> {
        let manifest = nzb::parse(document).expect("parse catalog NZB");
        catalog_manifest(
            &"ab".repeat(32),
            &manifest.nm1,
            &manifest.metadata,
            &manifest.files,
            None,
        )
        .expect("catalog manifest")
    }

    #[test]
    fn catalogs_typed_release_assets_with_stable_full_paths() {
        let assets = catalog(
            br#"<nzb>
                <file subject='"Season 01\Show.S01E02.mkv" yEnc'><segments><segment bytes="10" number="1">video</segment></segments></file>
                <file subject='"release.part01.rar" yEnc'><segments><segment bytes="11" number="1">rar</segment></segments></file>
                <file subject='"release.vol00+01.par2" yEnc'><segments><segment bytes="12" number="1">par2</segment></segments></file>
                <file subject='"release.001" yEnc'><segments><segment bytes="13" number="1">split</segment></segments></file>
                <file subject='"notes.txt" yEnc'><segments><segment bytes="14" number="1">text</segment></segments></file>
            </nzb>"#,
        );

        assert_eq!(assets.len(), 5);
        assert_eq!(assets[0].relative_path, "Season 01/Show.S01E02.mkv");
        assert_eq!(assets[0].kind, AssetKind::Video);
        assert_eq!(
            assets[0].asset_id,
            "5a4d3b85bd915b206df1dae1773db2eab9b10878a39918f87cf6abcf8b7943a1"
        );
        assert_eq!(assets[1].kind, AssetKind::Archive);
        assert_eq!(assets[2].kind, AssetKind::Par2);
        assert_eq!(assets[3].kind, AssetKind::Split);
        assert_eq!(assets[4].relative_path, "par2-source/0004");
        assert_eq!(assets[4].kind, AssetKind::Par2Source);
        assert!(assets.iter().all(|asset| asset.asset_id.len() == 64));
    }

    #[test]
    fn preserves_obfuscated_files_as_par2_sources_from_recovery_candidates() {
        let assets = catalog(
            br#"<nzb>
                <file subject='post [1/3] &quot;4f6a9d2c&quot; yEnc'><segments><segment bytes="100" number="1">source</segment></segments></file>
                <file subject='post [2/3] &quot;release.part01.rar&quot; yEnc'><segments><segment bytes="100" number="1">rar</segment></segments></file>
                <file subject='post [3/3] &quot;opaque-recoverypar2&quot; yEnc'><segments><segment bytes="12" number="1">recovery</segment></segments></file>
            </nzb>"#,
        );

        assert_eq!(assets.len(), 3);
        assert_eq!(assets[0].relative_path, "par2-source/0000");
        assert_eq!(assets[0].kind, AssetKind::Par2Source);
        assert_eq!(assets[1].kind, AssetKind::Archive);
        assert_eq!(assets[2].kind, AssetKind::Par2);
    }

    #[test]
    fn catalogs_unquoted_and_one_sided_quoted_subject_filenames() {
        let assets = catalog(
            br#"<nzb>
                <file subject="release Movie.2026.mp4&quot; yEnc (1/0)"><segments><segment bytes="300" number="1">video</segment></segments></file>
                <file subject="Movie.2026.vol000+001.par2 (1/0)"><segments><segment bytes="12" number="1">recovery</segment></segments></file>
            </nzb>"#,
        );

        assert_eq!(assets.len(), 2);
        assert_eq!(assets[0].relative_path, "Movie.2026.mp4");
        assert_eq!(assets[0].kind, AssetKind::Video);
        assert_eq!(assets[1].kind, AssetKind::Par2);
    }

    #[test]
    fn catalogs_metadata_video_and_opaque_recovery_candidates() {
        let assets = catalog(
            br#"<nzb>
                <head><meta type="title">Movie.2026.mkv</meta></head>
                <file subject="&quot;recovery-one&quot; yEnc"><segments><segment bytes="12" number="1">recovery-one</segment></segments></file>
                <file subject="&quot;video-source&quot; yEnc"><segments><segment bytes="300" number="1">video-source</segment></segments></file>
                <file subject="&quot;recovery-two&quot; yEnc"><segments><segment bytes="24" number="1">recovery-two</segment></segments></file>
            </nzb>"#,
        );

        assert_eq!(assets.len(), 3);
        assert_eq!(assets[0].relative_path, "par2-candidate/0000.par2");
        assert_eq!(assets[0].kind, AssetKind::Par2);
        assert_eq!(assets[1].relative_path, "Movie.2026.mkv");
        assert_eq!(assets[1].kind, AssetKind::Video);
        assert_eq!(assets[1].declared_bytes, 300);
        assert_eq!(assets[2].relative_path, "par2-candidate/0002.par2");
        assert_eq!(assets[2].kind, AssetKind::Par2);
    }

    #[test]
    fn catalogs_explicit_names_and_primary_ordinals_without_content_heuristics() {
        let assets = catalog(
            br#"<nzb>
                <file subject='"Movie.Sample.mkv" yEnc'><segments><segment bytes="1" number="1">junk</segment></segments></file>
                <file subject='"Movie.mkv" yEnc'>
                    <meta type="name">Other.mp4</meta>
                    <segments><segment bytes="1" number="1">ambiguous</segment></segments>
                </file>
                <file subject='"Movie.mkv" yEnc'><segments>
                    <segment bytes="100" number="1">primary</segment>
                    <segment bytes="900" number="1">fallback</segment>
                    <segment bytes="200" number="2">second</segment>
                </segments></file>
            </nzb>"#,
        );

        assert_eq!(assets.len(), 3);
        assert_eq!(assets[0].relative_path, "Movie.Sample.mkv");
        assert_eq!(assets[1].relative_path, "Other.mp4");
        assert_eq!(assets[2].relative_path, "Movie.mkv");
        assert_eq!(assets[2].declared_bytes, 300);
    }

    #[test]
    fn rejects_catalog_files_above_the_logical_media_limit() {
        let postings = (1..=(crate::limits::MAX_LOGICAL_BYTES / MAX_ARTICLE_BYTES + 1))
            .map(|number| nzb::Posting {
                number,
                bytes: MAX_ARTICLE_BYTES,
                message_id: "part@example.test".to_owned(),
            })
            .collect();
        let file = nzb::File {
            subject: Some("\"Movie.mkv\" yEnc".to_owned()),
            poster: None,
            date: None,
            groups: Vec::new(),
            metadata: std::collections::BTreeMap::new(),
            postings,
            first_segment_md5: None,
        };

        assert_eq!(declared_primary_bytes(&file), Err("asset_catalog_invalid"));
    }

    #[test]
    fn single_unnamed_file_uses_the_owned_video_selection_hint() {
        let manifest = nzb::parse(
            br#"<nzb><file subject="generic"><segments>
                <segment bytes="100" number="1">first</segment>
                <segment bytes="200" number="2">second</segment>
            </segments></file></nzb>"#,
        )
        .unwrap();
        let artifact = "ab".repeat(32);
        let hinted = catalog_manifest(
            &artifact,
            &manifest.nm1,
            &manifest.metadata,
            &manifest.files,
            Some(("Movie.2026.mkv", 300)),
        )
        .unwrap();

        assert_eq!(hinted.len(), 1);
        assert_eq!(hinted[0].relative_path, "Movie.2026.mkv");
        assert_eq!(hinted[0].declared_bytes, 300);
        let provider_sized = catalog_manifest(
            &artifact,
            &manifest.nm1,
            &manifest.metadata,
            &manifest.files,
            Some(("Movie.2026.mkv", 301)),
        )
        .unwrap();
        assert_eq!(provider_sized[0].declared_bytes, 301);
        let opaque_extension = catalog_manifest(
            &artifact,
            &manifest.nm1,
            &manifest.metadata,
            &manifest.files,
            Some(("Movie.2026.future", 300)),
        )
        .unwrap();
        assert_eq!(opaque_extension[0].relative_path, "Movie.2026.future");
        assert_eq!(opaque_extension[0].kind, AssetKind::Video);
        assert_eq!(
            catalog_manifest(
                &artifact,
                &manifest.nm1,
                &manifest.metadata,
                &manifest.files,
                Some(("../Movie.2026.mkv", 300)),
            ),
            Err("asset_catalog_invalid")
        );
        let with_recovery = nzb::parse(
            br#"<nzb>
                <file subject="generic"><segments><segment bytes="300" number="1">video</segment></segments></file>
                <file subject="&quot;release.par2&quot; yEnc"><segments><segment bytes="12" number="1">recovery</segment></segments></file>
            </nzb>"#,
        )
        .unwrap();
        let recovered = catalog_manifest(
            &artifact,
            &with_recovery.nm1,
            &with_recovery.metadata,
            &with_recovery.files,
            Some(("Movie.2026.mkv", 300)),
        )
        .unwrap();
        assert_eq!(recovered.len(), 2);
        assert_eq!(recovered[0].relative_path, "Movie.2026.mkv");
        assert_eq!(recovered[0].kind, AssetKind::Video);
        assert_eq!(recovered[1].kind, AssetKind::Par2);
        let ambiguous = nzb::parse(
            br#"<nzb><file subject="&quot;First.mkv&quot; &quot;Second.mp4&quot;">
                <segments><segment bytes="300" number="1">video</segment></segments>
            </file></nzb>"#,
        )
        .unwrap();
        assert!(
            catalog_manifest(
                &artifact,
                &ambiguous.nm1,
                &ambiguous.metadata,
                &ambiguous.files,
                Some(("Movie.2026.mkv", 300)),
            )
            .unwrap()
            .is_empty()
        );
    }

    #[test]
    fn catalogs_an_owned_opaque_logical_video_as_ordered_split_parts() {
        let manifest = nzb::parse(
            br#"<nzb>
                <file subject="cf6bef2a"><segments><segment bytes="100" number="1">first</segment></segments></file>
                <file subject="&quot;release.par2&quot; yEnc"><segments><segment bytes="12" number="1">recovery</segment></segments></file>
                <file subject="d25ea847"><segments><segment bytes="200" number="1">second</segment></segments></file>
                <file subject="98326af0"><segments><segment bytes="301" number="1">third</segment></segments></file>
            </nzb>"#,
        )
        .unwrap();
        let assets = catalog_manifest(
            &"ab".repeat(32),
            &manifest.nm1,
            &manifest.metadata,
            &manifest.files,
            Some(("Movie.2026.mkv", 600)),
        )
        .unwrap();

        assert_eq!(assets.len(), 4);
        assert_eq!(assets[0].relative_path, "Movie.2026.mkv/part.001");
        assert_eq!(assets[1].kind, AssetKind::Par2);
        assert_eq!(assets[2].relative_path, "Movie.2026.mkv/part.002");
        assert_eq!(assets[3].relative_path, "Movie.2026.mkv/part.003");
        assert_eq!(assets[3].declared_bytes, 301);
        assert!(
            assets
                .iter()
                .filter(|asset| asset.kind != AssetKind::Par2)
                .all(|asset| asset.kind == AssetKind::LogicalSplit)
        );
        let unhinted = catalog_manifest(
            &"ab".repeat(32),
            &manifest.nm1,
            &manifest.metadata,
            &manifest.files,
            None,
        )
        .unwrap();
        assert_eq!(unhinted.len(), 4);
        assert_eq!(unhinted[0].kind, AssetKind::Par2Source);
        assert_eq!(unhinted[1].kind, AssetKind::Par2);
        assert_eq!(unhinted[2].kind, AssetKind::Par2Source);
        assert_eq!(unhinted[3].kind, AssetKind::Par2Source);
    }

    #[test]
    fn catalogs_an_owned_obfuscated_archive_set_as_one_logical_video() {
        let manifest = nzb::parse(
            br#"<nzb>
                <file subject="&quot;jBGjsS2RQw2dBZDaRzaQ.rar&quot; yEnc"><segments><segment bytes="100" number="1">first</segment></segments></file>
                <file subject="&quot;release.par2&quot; yEnc"><segments><segment bytes="12" number="1">recovery</segment></segments></file>
                <file subject="&quot;pLWDL8l4JKTB-xyML50A9nkxRtx3_.rar&quot; yEnc"><segments><segment bytes="200" number="1">second</segment></segments></file>
            </nzb>"#,
        )
        .unwrap();
        let assets = catalog_manifest(
            &"ab".repeat(32),
            &manifest.nm1,
            &manifest.metadata,
            &manifest.files,
            Some(("Movie.2026.mkv", 250)),
        )
        .unwrap();

        assert_eq!(assets.len(), 3);
        assert_eq!(
            assets[0].relative_path,
            "Movie.2026.mkv/archive/jBGjsS2RQw2dBZDaRzaQ.rar"
        );
        assert_eq!(assets[1].kind, AssetKind::Par2);
        assert_eq!(
            assets[2].relative_path,
            "Movie.2026.mkv/archive/pLWDL8l4JKTB-xyML50A9nkxRtx3_.rar"
        );
        assert!(
            assets
                .iter()
                .filter(|asset| asset.kind != AssetKind::Par2)
                .all(|asset| asset.kind == AssetKind::LogicalArchive)
        );
    }

    #[test]
    fn catalog_revalidates_identity_ordinals_and_path_uniqueness() {
        let mut manifest = nzb::parse(
            br#"<nzb>
                <file subject='"same/Movie.mkv"'><segments><segment bytes="1" number="1">one</segment></segments></file>
                <file subject='"SAME/movie.MKV"'><segments><segment bytes="1" number="1">two</segment></segments></file>
            </nzb>"#,
        )
        .unwrap();
        assert_eq!(
            catalog_manifest(
                &"ab".repeat(32),
                &manifest.nm1,
                &manifest.metadata,
                &manifest.files,
                None,
            ),
            Err("asset_catalog_path_conflict")
        );
        assert_eq!(
            catalog_manifest(
                &"ab".repeat(32),
                &format!("nm1:{}", "0".repeat(64)),
                &manifest.metadata,
                &manifest.files,
                None,
            ),
            Err("asset_catalog_invalid")
        );

        manifest.files[0].postings[0].number = 2;
        let identity = nzb::manifest_identity(&manifest.metadata, &manifest.files);
        assert_eq!(
            catalog_manifest(
                &"ab".repeat(32),
                &identity,
                &manifest.metadata,
                &manifest.files,
                None,
            ),
            Err("asset_catalog_invalid")
        );
    }

    #[test]
    fn validates_fast_start_mp4_and_extracts_mvhd_duration() {
        let mut head = iso_box(b"ftyp", b"isom\0\0\0\0isommp42");
        head.extend_from_slice(&iso_box(b"moov", &mvhd(1000, 90_500)));

        let evidence = probe_container(&head, &[]).unwrap();

        assert_eq!(evidence.kind, ContainerKind::Mp4);
        assert_eq!(evidence.duration_millis, Some(90_500));
        assert_eq!(evidence.inspected_head_bytes, head.len());
        assert_eq!(evidence.inspected_tail_bytes, 0);
    }

    #[test]
    fn validates_tail_moov_after_an_arbitrary_tail_prefix() {
        let head = iso_box(b"ftyp", b"isom\0\0\0\0isommp42");
        let mut tail = vec![0x55; 31];
        tail.extend_from_slice(&iso_box(b"moov", &mvhd(24_000, 48_000)));

        let evidence = probe_container(&head, &tail).unwrap();

        assert_eq!(evidence.kind, ContainerKind::Mp4);
        assert_eq!(evidence.duration_millis, Some(2_000));
    }

    #[test]
    fn validates_matroska_ebml_segment_info_and_duration() {
        let head = ebml_container(b"matroska", &[0x0f, 0x42, 0x40], 120_500_f64);

        let evidence = probe_container(&head, &[]).unwrap();

        assert_eq!(evidence.kind, ContainerKind::Matroska);
        assert_eq!(evidence.duration_millis, Some(120_500));
    }

    #[test]
    fn recognizes_webm_and_quicktime_container_variants() {
        let webm = ebml_container(b"webm", &[0x0f, 0x42, 0x40], 42_f64);
        assert_eq!(
            probe_container(&webm, &[]).unwrap().kind,
            ContainerKind::WebM
        );

        let mut quicktime = iso_box(b"ftyp", b"qt  \0\0\0\0qt  ");
        quicktime.extend_from_slice(&iso_box(b"moov", &mvhd(1000, 1000)));
        assert_eq!(
            probe_container(&quicktime, &[]).unwrap().kind,
            ContainerKind::QuickTime
        );
    }

    #[test]
    fn accepts_opaque_iso_brands_but_rejects_invalid_scale_and_false_tail_boxes() {
        let mut opaque_brand = iso_box(b"ftyp", b"zzzz\0\0\0\0zzzz");
        opaque_brand.extend_from_slice(&iso_box(b"moov", &mvhd(1000, 1000)));
        assert_eq!(
            probe_container(&opaque_brand, &[]).unwrap().kind,
            ContainerKind::Mp4
        );

        let invalid_scale = ebml_container(b"matroska", &[0], 42_f64);
        assert_eq!(
            probe_container(&invalid_scale, &[]),
            Err("container_structure_invalid")
        );

        let head = iso_box(b"ftyp", b"isom\0\0\0\0isom");
        let evidence = probe_container(&head, b"noise\0\0\0\x10moovtruncated").unwrap();
        assert_eq!(evidence.kind, ContainerKind::Mp4);
        assert_eq!(evidence.duration_millis, None);
    }

    #[test]
    fn detects_without_a_filename_and_rejects_truncation_and_budget_overflow() {
        let head = iso_box(b"ftyp", b"isom\0\0\0\0isommp42");
        let evidence = probe_container(&head, &[]).unwrap();
        assert_eq!(evidence.kind, ContainerKind::Mp4);
        assert_eq!(evidence.duration_millis, None);

        let mut complete = head;
        complete.extend_from_slice(&iso_box(b"moov", &mvhd(1000, 1000)));
        assert_eq!(
            probe_container(&complete, &[]).unwrap().kind,
            ContainerKind::Mp4
        );
        assert_eq!(
            probe_container(&complete[..7], &[]),
            Err("container_signature_mismatch")
        );
        assert_eq!(
            probe_container(&vec![0; 2 * 1024 * 1024 + 1], &[]),
            Err("container_probe_budget")
        );
    }
}
