use crate::archive::{
    ArchiveEvidence, ArchiveFormat, VolumeEndEvidence, VolumeHint, VolumeScheme,
    classify_volume_name, detect_archive, detect_volume_end, normalize_archive_path,
};
use serde::Serialize;
use sha2::{Digest, Sha256};

pub const MAX_VOLUMES: usize = crate::nzb::MAX_FILES;
const MAX_EVIDENCE_BYTES: usize = 8 * 1024 * 1024;
const MAX_SAMPLE_BYTES: usize = 64 * 1024;
pub use crate::limits::MAX_LOGICAL_BYTES;

pub fn sample_bytes_per_volume(volume_count: usize) -> usize {
    MAX_SAMPLE_BYTES.min(MAX_EVIDENCE_BYTES / volume_count.max(1).saturating_mul(2))
}

#[derive(Clone, Copy)]
pub struct VolumeInput<'a> {
    pub content_identity: &'a str,
    pub relative_path: &'a str,
    pub exact_size: u64,
    pub head: &'a [u8],
    pub tail: &'a [u8],
}

#[derive(Clone, Copy)]
pub struct RarHeaderVolumeInput<'a> {
    pub content_identity: &'a str,
    pub relative_path: &'a str,
    pub exact_size: u64,
    pub evidence: ArchiveEvidence,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(tag = "layout", content = "format", rename_all = "snake_case")]
pub enum VolumePlanKind {
    SingleArchive(ArchiveFormat),
    MultiVolumeArchive(ArchiveFormat),
    RawSplit,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct PlannedVolume {
    pub content_identity: String,
    pub relative_path: String,
    pub number: u32,
    pub exact_size: u64,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct VolumePlan {
    pub set_identity: String,
    pub kind: VolumePlanKind,
    pub exact_size: u64,
    pub volumes: Vec<PlannedVolume>,
}

struct Entry<'a> {
    content_identity: String,
    relative_path: String,
    exact_size: u64,
    evidence: Option<ArchiveEvidence>,
    hint: Option<VolumeHint>,
    tail: &'a [u8],
    ending: Option<VolumeEndEvidence>,
}

pub fn plan_volumes(inputs: &[VolumeInput<'_>]) -> Result<VolumePlan, &'static str> {
    let mut evidence_bytes = 0_usize;
    let mut entries = Vec::with_capacity(inputs.len());
    for input in inputs {
        evidence_bytes = evidence_bytes
            .checked_add(input.head.len())
            .and_then(|value| value.checked_add(input.tail.len()))
            .ok_or("archive_volume_budget")?;
        if evidence_bytes > MAX_EVIDENCE_BYTES {
            return Err("archive_volume_budget");
        }
        let evidence = detect_archive(input.head)?;
        entries.push(Entry {
            content_identity: input.content_identity.to_owned(),
            hint: classify_volume_name(input.relative_path),
            relative_path: input.relative_path.to_owned(),
            exact_size: input.exact_size,
            evidence,
            tail: input.tail,
            ending: None,
        });
    }

    if entries.iter().any(|entry| {
        entry
            .evidence
            .is_some_and(|value| value.format == ArchiveFormat::Zip && value.volume_layout)
    }) {
        return Err("archive_layout_unsupported");
    }
    if entries.iter().all(|entry| {
        entry
            .evidence
            .is_some_and(|value| matches!(value.format, ArchiveFormat::Rar4 | ArchiveFormat::Rar5))
    }) {
        if entries.len() == 1
            && !entries[0]
                .evidence
                .expect("checked RAR evidence")
                .volume_layout
        {
            return plan_single(entries.pop().expect("one RAR archive entry"));
        }
        let encrypted_headers = entries.iter().all(|entry| {
            entry
                .evidence
                .is_some_and(|evidence| evidence.encrypted_header)
        });
        return plan_rar(entries, !encrypted_headers);
    }
    if entries.iter().all(|entry| {
        entry.hint.as_ref().is_some_and(|hint| {
            matches!(
                hint.scheme,
                VolumeScheme::SevenZipSplit | VolumeScheme::NumericSplit
            )
        })
    }) && entries.iter().any(|entry| {
        entry
            .evidence
            .is_some_and(|value| value.format == ArchiveFormat::SevenZip)
    }) {
        return plan_seven_zip(entries);
    }
    if entries.iter().all(|entry| {
        entry
            .hint
            .as_ref()
            .is_some_and(|hint| hint.scheme == VolumeScheme::NumericSplit)
            && entry.evidence.is_none()
    }) {
        return plan_raw_split(entries);
    }
    if entries.len() == 1 {
        return plan_single(entries.pop().expect("one archive entry"));
    }
    Err("archive_volume_conflict")
}

pub fn plan_rar_headers(inputs: &[RarHeaderVolumeInput<'_>]) -> Result<VolumePlan, &'static str> {
    let mut entries = Vec::with_capacity(inputs.len());
    for input in inputs {
        if !matches!(
            input.evidence.format,
            ArchiveFormat::Rar4 | ArchiveFormat::Rar5
        ) {
            return Err("archive_volume_conflict");
        }
        entries.push(Entry {
            content_identity: input.content_identity.to_owned(),
            hint: classify_volume_name(input.relative_path),
            relative_path: input.relative_path.to_owned(),
            exact_size: input.exact_size,
            evidence: Some(input.evidence),
            tail: b"",
            ending: None,
        });
    }
    plan_rar(entries, false)
}

pub fn member_identity(
    set_identity: &str,
    relative_path: &str,
    exact_size: u64,
) -> Result<String, &'static str> {
    if !valid_identity(set_identity) || exact_size == 0 {
        return Err("archive_catalog_invalid");
    }
    let normalized =
        normalize_archive_path(relative_path).map_err(|_| "archive_catalog_invalid")?;
    if normalized != relative_path {
        return Err("archive_catalog_invalid");
    }
    let encoded = relative_path.as_bytes();
    let mut digest = Sha256::new();
    digest.update(b"comet-archive-member-v1\0");
    digest.update(set_identity.as_bytes());
    digest.update(
        u32::try_from(encoded.len())
            .map_err(|_| "archive_catalog_invalid")?
            .to_be_bytes(),
    );
    digest.update(encoded);
    digest.update(exact_size.to_be_bytes());
    Ok(format!("{:x}", digest.finalize()))
}

fn plan_rar(
    mut entries: Vec<Entry<'_>>,
    require_terminal_evidence: bool,
) -> Result<VolumePlan, &'static str> {
    let format = entries[0].evidence.expect("checked RAR evidence").format;
    if entries
        .iter()
        .any(|entry| entry.evidence.expect("checked RAR evidence").format != format)
    {
        return Err("archive_volume_conflict");
    }
    if entries.len() == 1
        && entries[0]
            .evidence
            .expect("checked RAR evidence")
            .volume_layout
    {
        return Err("archive_volume_gap");
    }
    let header_numbered = entries.iter().all(|entry| {
        effective_volume_number(entry.evidence.expect("checked RAR evidence")).is_some()
    });
    let name_scheme = coherent_name_scheme(&entries);
    let needs_terminal_evidence =
        require_terminal_evidence && !header_numbered && name_scheme.is_none();
    if needs_terminal_evidence {
        for entry in &mut entries {
            entry.ending =
                Some(detect_volume_end(format, entry.tail)?.expect("RAR ending evidence"));
        }
    }
    let ending_numbered = needs_terminal_evidence
        && entries.iter().all(|entry| {
            entry
                .ending
                .and_then(|ending| ending.volume_number)
                .is_some()
        });
    if needs_terminal_evidence
        && !ending_numbered
        && entries.iter().any(|entry| entry.hint.is_some())
    {
        return Err("archive_volume_conflict");
    }
    if header_numbered {
        entries.sort_by_key(|entry| {
            effective_volume_number(entry.evidence.expect("checked RAR evidence"))
                .expect("checked RAR volume number")
        });
    } else if ending_numbered {
        entries.sort_by_key(|entry| {
            entry
                .ending
                .and_then(|ending| ending.volume_number)
                .expect("checked RAR end volume number")
        });
    } else if let Some(scheme) = name_scheme {
        if !matches!(
            scheme,
            VolumeScheme::RarPart | VolumeScheme::RarLegacy | VolumeScheme::NumericSplit
        ) || entries.iter().any(|entry| {
            name_number(entry.hint.as_ref().expect("coherent RAR name"), scheme).is_none()
        }) {
            return Err("archive_volume_gap");
        }
        entries.sort_by_key(|entry| {
            name_number(entry.hint.as_ref().expect("coherent RAR name"), scheme)
                .expect("coherent RAR number")
        });
    } else if entries.len() > 1 {
        return Err("archive_identity_unproven");
    }
    let any_first_volume = entries.iter().any(|candidate| {
        candidate
            .evidence
            .expect("checked RAR evidence")
            .first_volume
    });
    for (index, entry) in entries.iter().enumerate() {
        let expected = u32::try_from(index).expect("bounded archive volume count");
        let evidence = entry.evidence.expect("checked RAR evidence");
        if entries.len() > 1 && !evidence.volume_layout {
            return Err("archive_volume_conflict");
        }
        if let Some(number) = effective_volume_number(evidence)
            && number != expected
        {
            return Err("archive_volume_gap");
        }
        if !header_numbered
            && !ending_numbered
            && let Some(scheme) = name_scheme
            && name_number(
                entry.hint.as_ref().ok_or("archive_volume_conflict")?,
                scheme,
            ) != Some(expected)
        {
            return Err("archive_volume_gap");
        }
        if any_first_volume && evidence.first_volume != (index == 0) {
            return Err("archive_volume_conflict");
        }
        if needs_terminal_evidence {
            let ending = entry.ending.expect("checked RAR ending evidence");
            if ending.next_volume != (index + 1 < entries.len())
                || ending
                    .volume_number
                    .is_some_and(|number| number != expected)
            {
                return Err("archive_volume_gap");
            }
        }
    }
    let kind = if entries.len() == 1 {
        VolumePlanKind::SingleArchive(format)
    } else {
        VolumePlanKind::MultiVolumeArchive(format)
    };
    Ok(finish(kind, entries))
}

fn plan_seven_zip(mut entries: Vec<Entry<'_>>) -> Result<VolumePlan, &'static str> {
    let scheme = coherent_name_scheme(&entries).ok_or("archive_volume_conflict")?;
    if !matches!(
        scheme,
        VolumeScheme::SevenZipSplit | VolumeScheme::NumericSplit
    ) || entries[0]
        .hint
        .as_ref()
        .expect("coherent 7-Zip name")
        .base
        .is_empty()
    {
        return Err("archive_volume_conflict");
    }
    entries.sort_by_key(|entry| entry.hint.as_ref().expect("7-Zip name").number);
    for (index, entry) in entries.iter().enumerate() {
        let expected = u32::try_from(index + 1).expect("bounded archive volume count");
        if entry.hint.as_ref().expect("7-Zip name").number != expected {
            return Err("archive_volume_gap");
        }
        if index == 0 {
            if entry
                .evidence
                .is_none_or(|value| value.format != ArchiveFormat::SevenZip)
            {
                return Err("archive_identity_unproven");
            }
        } else if entry.evidence.is_some() {
            return Err("archive_volume_conflict");
        }
    }
    let expected_size = entries[0]
        .evidence
        .expect("checked 7-Zip evidence")
        .logical_size
        .ok_or("archive_header_invalid")?;
    let exact_size = entries.iter().map(|entry| entry.exact_size).sum::<u64>();
    if exact_size != expected_size {
        return Err("archive_volume_gap");
    }
    Ok(finish(
        VolumePlanKind::MultiVolumeArchive(ArchiveFormat::SevenZip),
        entries,
    ))
}

fn plan_raw_split(mut entries: Vec<Entry<'_>>) -> Result<VolumePlan, &'static str> {
    if entries.len() < 2 {
        return Err("archive_volume_gap");
    }
    let scheme = coherent_name_scheme(&entries).ok_or("archive_volume_conflict")?;
    if scheme != VolumeScheme::NumericSplit {
        return Err("archive_volume_conflict");
    }
    entries.sort_by_key(|entry| entry.hint.as_ref().expect("raw split name").number);
    for (index, entry) in entries.iter().enumerate() {
        let expected = u32::try_from(index + 1).expect("bounded archive volume count");
        if entry.hint.as_ref().expect("raw split name").number != expected {
            return Err("archive_volume_gap");
        }
    }
    Ok(finish(VolumePlanKind::RawSplit, entries))
}

fn plan_single(entry: Entry<'_>) -> Result<VolumePlan, &'static str> {
    let evidence = entry.evidence.ok_or("archive_identity_unproven")?;
    if evidence.volume_layout {
        return Err("archive_volume_gap");
    }
    if evidence.format == ArchiveFormat::SevenZip && evidence.logical_size != Some(entry.exact_size)
    {
        return Err("archive_volume_gap");
    }
    Ok(finish(
        VolumePlanKind::SingleArchive(evidence.format),
        vec![entry],
    ))
}

fn coherent_name_scheme(entries: &[Entry<'_>]) -> Option<VolumeScheme> {
    let first = entries.first()?.hint.as_ref()?;
    entries
        .iter()
        .all(|entry| {
            entry
                .hint
                .as_ref()
                .is_some_and(|hint| hint.base == first.base && hint.scheme == first.scheme)
        })
        .then_some(first.scheme)
}

fn name_number(hint: &VolumeHint, scheme: VolumeScheme) -> Option<u32> {
    match scheme {
        VolumeScheme::RarPart | VolumeScheme::NumericSplit => hint.number.checked_sub(1),
        VolumeScheme::RarLegacy => Some(hint.number),
        _ => None,
    }
}

fn effective_volume_number(evidence: ArchiveEvidence) -> Option<u32> {
    if evidence.format == ArchiveFormat::Rar5 && evidence.first_volume {
        Some(0)
    } else {
        evidence.volume_number
    }
}

fn finish(kind: VolumePlanKind, entries: Vec<Entry<'_>>) -> VolumePlan {
    let exact_size = entries.iter().map(|entry| entry.exact_size).sum();
    let mut digest = Sha256::new();
    digest.update(b"comet-archive-volume-set-v1\0");
    digest.update([match kind {
        VolumePlanKind::SingleArchive(format) | VolumePlanKind::MultiVolumeArchive(format) => {
            format_tag(format)
        }
        VolumePlanKind::RawSplit => 0,
    }]);
    let volumes = entries
        .into_iter()
        .enumerate()
        .map(|(index, entry)| {
            digest.update(entry.content_identity.as_bytes());
            digest.update(entry.exact_size.to_be_bytes());
            PlannedVolume {
                content_identity: entry.content_identity,
                relative_path: entry.relative_path,
                number: u32::try_from(index).expect("bounded archive volume count"),
                exact_size: entry.exact_size,
            }
        })
        .collect();
    VolumePlan {
        set_identity: format!("{:x}", digest.finalize()),
        kind,
        exact_size,
        volumes,
    }
}

fn valid_identity(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

const fn format_tag(format: ArchiveFormat) -> u8 {
    match format {
        ArchiveFormat::Rar4 => 1,
        ArchiveFormat::Rar5 => 2,
        ArchiveFormat::SevenZip => 3,
        ArchiveFormat::Zip => 4,
        ArchiveFormat::Gzip => 5,
        ArchiveFormat::Tar => 6,
    }
}

#[cfg(test)]
mod tests {
    use super::{
        RarHeaderVolumeInput, VolumeInput, VolumePlanKind, member_identity, plan_rar_headers,
        plan_volumes, sample_bytes_per_volume,
    };
    use crate::archive::{ArchiveFormat, detect_archive};

    #[test]
    fn evidence_sampling_scales_with_the_volume_count() {
        assert_eq!(sample_bytes_per_volume(1), 64 * 1024);
        assert!(sample_bytes_per_volume(196) > 16 * 1024);
        assert!(sample_bytes_per_volume(196) * 196 * 2 <= 8 * 1024 * 1024);
    }

    fn seven_zip(logical_size: u64) -> Vec<u8> {
        let mut header = b"\x37\x7a\xbc\xaf\x27\x1c\0\x04".to_vec();
        header.extend_from_slice(&[0; 24]);
        header[12..20].copy_from_slice(&(logical_size - 32).to_le_bytes());
        let crc = crc32fast::hash(&header[12..32]);
        header[8..12].copy_from_slice(&crc.to_le_bytes());
        header
    }

    fn rar5_main(number: Option<u8>) -> Vec<u8> {
        let mut header = b"Rar!\x1a\x07\x01\0".to_vec();
        header.extend_from_slice(&[0; 4]);
        let header_size = if number.is_some() { 4 } else { 3 };
        header.extend_from_slice(&[header_size, 1, 0]);
        header.push(if number.is_some() { 3 } else { 1 });
        if let Some(number) = number {
            header.push(number);
        }
        let crc = crc32fast::hash(&header[12..]);
        header[8..12].copy_from_slice(&crc.to_le_bytes());
        header
    }

    fn rar5_single() -> Vec<u8> {
        let mut header = b"Rar!\x1a\x07\x01\0".to_vec();
        header.extend_from_slice(&[0; 4]);
        header.extend_from_slice(&[3, 1, 0, 0]);
        let crc = crc32fast::hash(&header[12..]);
        header[8..12].copy_from_slice(&crc.to_le_bytes());
        header
    }

    fn rar5_encrypted() -> Vec<u8> {
        let mut header = b"Rar!\x1a\x07\x01\0".to_vec();
        header.extend_from_slice(&[0; 4]);
        let body = [4, 0, 0, 1, 15, 0, 0, 0];
        header.push(body.len() as u8);
        header.extend_from_slice(&body);
        let crc = crc32fast::hash(&header[12..]);
        header[8..12].copy_from_slice(&crc.to_le_bytes());
        header
    }

    fn rar5_end(next_volume: bool) -> Vec<u8> {
        let mut header = vec![0; 4];
        header.extend_from_slice(&[3, 5, 0, u8::from(next_volume)]);
        let crc = crc32fast::hash(&header[4..]);
        header[..4].copy_from_slice(&crc.to_le_bytes());
        header
    }

    fn encrypted_zip() -> Vec<u8> {
        let mut header = vec![0_u8; 30];
        header[..4].copy_from_slice(b"PK\x03\x04");
        header[6..8].copy_from_slice(&1_u16.to_le_bytes());
        header[26..28].copy_from_slice(&4_u16.to_le_bytes());
        header.extend_from_slice(b"name");
        header
    }

    fn rar4_main(first_volume: bool) -> Vec<u8> {
        let mut header = b"Rar!\x1a\x07\0".to_vec();
        let flags = 0x0001 | if first_volume { 0x0100 } else { 0 };
        header.extend_from_slice(&[0, 0, 0x73]);
        header.extend_from_slice(&u16::to_le_bytes(flags));
        header.extend_from_slice(&13_u16.to_le_bytes());
        header.extend_from_slice(&[0; 6]);
        let crc = crc32fast::hash(&header[9..]) as u16;
        header[7..9].copy_from_slice(&crc.to_le_bytes());
        header
    }

    fn rar4_end(next_volume: bool, number: u16, reserve_space: bool) -> Vec<u8> {
        let flags =
            0x0008 | if next_volume { 0x0001 } else { 0 } | if reserve_space { 0x0004 } else { 0 };
        let header_size = 9 + if reserve_space { 7 } else { 0 };
        let mut header = vec![0, 0, 0x7b];
        header.extend_from_slice(&u16::to_le_bytes(flags));
        header.extend_from_slice(&u16::to_le_bytes(header_size));
        header.extend_from_slice(&number.to_le_bytes());
        if reserve_space {
            header.extend_from_slice(&[0; 7]);
        }
        let crc = crc32fast::hash(&header[2..]) as u16;
        header[..2].copy_from_slice(&crc.to_le_bytes());
        header
    }

    #[test]
    fn groups_header_numbered_terminal_proven_rar_volumes() {
        let first_identity = "a".repeat(64);
        let second_identity = "b".repeat(64);
        let first_head = rar5_main(None);
        let second_head = rar5_main(Some(1));
        let first_tail = rar5_end(true);
        let second_tail = rar5_end(false);
        let inputs = [
            VolumeInput {
                content_identity: &first_identity,
                relative_path: "5fc6f2a1.data",
                exact_size: 100,
                head: &first_head,
                tail: &first_tail,
            },
            VolumeInput {
                content_identity: &second_identity,
                relative_path: "c90b7e34.data",
                exact_size: 200,
                head: &second_head,
                tail: &second_tail,
            },
        ];

        let plan = plan_volumes(&inputs).expect("plan RAR5 volumes");

        assert_eq!(
            plan.kind,
            VolumePlanKind::MultiVolumeArchive(ArchiveFormat::Rar5)
        );
        assert_eq!(plan.exact_size, 300);
        assert_eq!(plan.volumes[0].number, 0);
        assert_eq!(plan.volumes[1].number, 1);
        assert_eq!(plan.set_identity.len(), 64);
    }

    #[test]
    fn encrypted_single_archives_remain_plannable_for_the_password_aware_runtime() {
        let identity = "a".repeat(64);
        let head = encrypted_zip();
        let input = VolumeInput {
            content_identity: &identity,
            relative_path: "release.zip",
            exact_size: head.len() as u64,
            head: &head,
            tail: &[],
        };

        let plan = plan_volumes(&[input]).expect("plan encrypted archive");

        assert_eq!(plan.kind, VolumePlanKind::SingleArchive(ArchiveFormat::Zip));
    }

    #[test]
    fn encrypted_rar_volumes_use_the_coherent_filename_sequence() {
        let first_identity = "a".repeat(64);
        let second_identity = "b".repeat(64);
        let head = rar5_encrypted();
        let inputs = [
            VolumeInput {
                content_identity: &second_identity,
                relative_path: "release.part02.rar",
                exact_size: 200,
                head: &head,
                tail: &[],
            },
            VolumeInput {
                content_identity: &first_identity,
                relative_path: "release.part01.rar",
                exact_size: 100,
                head: &head,
                tail: &[],
            },
        ];

        let plan = plan_volumes(&inputs).expect("plan encrypted RAR volumes");

        assert_eq!(
            plan.kind,
            VolumePlanKind::MultiVolumeArchive(ArchiveFormat::Rar5)
        );
        assert_eq!(plan.volumes[0].content_identity, first_identity);
        assert_eq!(plan.volumes[1].content_identity, second_identity);
        let evidence = detect_archive(&head)
            .unwrap()
            .expect("encrypted RAR header evidence");
        let header_inputs = [
            RarHeaderVolumeInput {
                content_identity: &second_identity,
                relative_path: "release.part02.rar",
                exact_size: 200,
                evidence,
            },
            RarHeaderVolumeInput {
                content_identity: &first_identity,
                relative_path: "release.part01.rar",
                exact_size: 100,
                evidence,
            },
        ];
        assert_eq!(
            plan_rar_headers(&header_inputs)
                .expect("plan encrypted RAR headers")
                .volumes[0]
                .content_identity,
            first_identity
        );
    }

    #[test]
    fn plans_obfuscated_remote_rar_from_compact_header_evidence() {
        let first_identity = "a".repeat(64);
        let second_identity = "b".repeat(64);
        let first_head = rar5_main(None);
        let second_head = rar5_main(Some(1));
        let first_evidence = detect_archive(&first_head)
            .unwrap()
            .expect("first RAR header");
        let second_evidence = detect_archive(&second_head)
            .unwrap()
            .expect("second RAR header");
        let inputs = [
            RarHeaderVolumeInput {
                content_identity: &second_identity,
                relative_path: "random-b",
                exact_size: 200,
                evidence: second_evidence,
            },
            RarHeaderVolumeInput {
                content_identity: &first_identity,
                relative_path: "random-a",
                exact_size: 100,
                evidence: first_evidence,
            },
        ];

        let plan = plan_rar_headers(&inputs).expect("plan remote RAR headers");

        assert_eq!(
            plan.kind,
            VolumePlanKind::MultiVolumeArchive(ArchiveFormat::Rar5)
        );
        assert_eq!(plan.volumes[0].content_identity, first_identity);
        assert_eq!(plan.volumes[1].content_identity, second_identity);
    }

    #[test]
    fn header_numbered_rar_ignores_obfuscated_volume_names() {
        let first_identity = "a".repeat(64);
        let second_identity = "b".repeat(64);
        let first_head = rar5_main(None);
        let second_head = rar5_main(Some(1));
        let first_tail = rar5_end(true);
        let second_tail = rar5_end(false);
        let inputs = [
            VolumeInput {
                content_identity: &second_identity,
                relative_path: "Movie.mkv/archive/random-b.rar",
                exact_size: 200,
                head: &second_head,
                tail: &second_tail,
            },
            VolumeInput {
                content_identity: &first_identity,
                relative_path: "Movie.mkv/archive/random-a.rar",
                exact_size: 100,
                head: &first_head,
                tail: &first_tail,
            },
        ];

        let plan = plan_volumes(&inputs).expect("plan header-numbered obfuscated RAR");

        assert_eq!(
            plan.kind,
            VolumePlanKind::MultiVolumeArchive(ArchiveFormat::Rar5)
        );
        assert_eq!(plan.volumes[0].content_identity, first_identity);
        assert_eq!(plan.volumes[1].content_identity, second_identity);
    }

    #[test]
    fn binds_archive_members_to_the_exact_set_path_and_size() {
        let set_identity = "a".repeat(64);
        let first = member_identity(&set_identity, "Season 01/Episode.mkv", 42)
            .expect("derive member identity");
        assert_eq!(first.len(), 64);
        assert_eq!(
            first,
            member_identity(&set_identity, "Season 01/Episode.mkv", 42)
                .expect("derive stable member identity")
        );
        assert_ne!(
            first,
            member_identity(&set_identity, "Season 01/Episode.mkv", 43).expect("bind member size")
        );
        assert_eq!(
            member_identity(&set_identity, "../escape.mkv", 42),
            Err("archive_catalog_invalid")
        );
    }

    #[test]
    fn rejects_rar_number_gaps_without_requiring_redundant_terminal_proof() {
        let first_identity = "a".repeat(64);
        let second_identity = "b".repeat(64);
        let first_head = rar5_main(None);
        let wrong_head = rar5_main(Some(2));
        let first_tail = rar5_end(true);
        let final_tail = rar5_end(false);
        let wrong_number = [
            VolumeInput {
                content_identity: &first_identity,
                relative_path: "release.part01.rar",
                exact_size: 100,
                head: &first_head,
                tail: &first_tail,
            },
            VolumeInput {
                content_identity: &second_identity,
                relative_path: "release.part02.rar",
                exact_size: 100,
                head: &wrong_head,
                tail: &final_tail,
            },
        ];
        assert_eq!(plan_volumes(&wrong_number[..1]), Err("archive_volume_gap"));
        assert_eq!(plan_volumes(&wrong_number), Err("archive_volume_gap"));
        let second_head = rar5_main(Some(1));
        let wrong_terminal = [
            VolumeInput {
                content_identity: &first_identity,
                relative_path: "release.part01.rar",
                exact_size: 100,
                head: &first_head,
                tail: &first_tail,
            },
            VolumeInput {
                content_identity: &second_identity,
                relative_path: "release.part02.rar",
                exact_size: 100,
                head: &second_head,
                tail: &first_tail,
            },
        ];
        assert!(plan_volumes(&wrong_terminal).is_ok());
    }

    #[test]
    fn accepts_a_single_rar_from_header_evidence_without_a_name_hint() {
        let identity = "7".repeat(64);
        let head = rar5_single();
        let input = [VolumeInput {
            content_identity: &identity,
            relative_path: "obfuscated.data",
            exact_size: 100,
            head: &head,
            tail: b"",
        }];

        assert_eq!(
            plan_volumes(&input).expect("plan single RAR").kind,
            VolumePlanKind::SingleArchive(ArchiveFormat::Rar5)
        );
    }

    #[test]
    fn groups_rar4_from_terminal_numbers_without_name_assumptions() {
        let first_identity = "4".repeat(64);
        let second_identity = "5".repeat(64);
        let first_head = rar4_main(true);
        let second_head = rar4_main(false);
        let first_tail = rar4_end(true, 0, true);
        let second_tail = rar4_end(false, 1, false);
        let inputs = [
            VolumeInput {
                content_identity: &first_identity,
                relative_path: "release.rar",
                exact_size: 100,
                head: &first_head,
                tail: &first_tail,
            },
            VolumeInput {
                content_identity: &second_identity,
                relative_path: "release.r00",
                exact_size: 100,
                head: &second_head,
                tail: &second_tail,
            },
        ];

        assert_eq!(
            plan_volumes(&inputs).expect("plan legacy RAR4").kind,
            VolumePlanKind::MultiVolumeArchive(ArchiveFormat::Rar4)
        );
        let obfuscated = [
            VolumeInput {
                relative_path: "opaque-b",
                ..inputs[0]
            },
            VolumeInput {
                relative_path: "opaque-a",
                ..inputs[1]
            },
        ];
        let plan = plan_volumes(&obfuscated).expect("plan terminal-numbered obfuscated RAR4");
        assert_eq!(plan.volumes[0].content_identity, first_identity);
        assert_eq!(plan.volumes[1].content_identity, second_identity);
    }

    #[test]
    fn groups_only_complete_size_proven_split_7zip_sets() {
        let first_identity = "c".repeat(64);
        let second_identity = "d".repeat(64);
        let first = seven_zip(60);
        let inputs = [
            VolumeInput {
                content_identity: &first_identity,
                relative_path: "release.7z.001",
                exact_size: 40,
                head: &first,
                tail: b"",
            },
            VolumeInput {
                content_identity: &second_identity,
                relative_path: "release.7z.002",
                exact_size: 20,
                head: b"continuation",
                tail: b"",
            },
        ];

        assert_eq!(
            plan_volumes(&inputs).expect("plan split 7-Zip").kind,
            VolumePlanKind::MultiVolumeArchive(ArchiveFormat::SevenZip)
        );
        let mut incomplete = inputs;
        incomplete[1].exact_size = 19;
        assert_eq!(plan_volumes(&incomplete), Err("archive_volume_gap"));
        let independent = seven_zip(80);
        let independent_later_volume = [
            VolumeInput {
                exact_size: 40,
                head: &independent,
                ..inputs[0]
            },
            VolumeInput {
                exact_size: 40,
                head: &independent,
                ..inputs[1]
            },
        ];
        assert_eq!(
            plan_volumes(&independent_later_volume),
            Err("archive_volume_conflict")
        );
    }

    #[test]
    fn treats_numeric_parts_as_raw_only_without_archive_magic() {
        let first_identity = "e".repeat(64);
        let second_identity = "f".repeat(64);
        let inputs = [
            VolumeInput {
                content_identity: &first_identity,
                relative_path: "movie.mkv.001",
                exact_size: 10,
                head: b"raw first",
                tail: b"",
            },
            VolumeInput {
                content_identity: &second_identity,
                relative_path: "movie.mkv.002",
                exact_size: 12,
                head: b"raw second",
                tail: b"",
            },
        ];

        let plan = plan_volumes(&inputs).expect("plan raw split");

        assert_eq!(plan.kind, VolumePlanKind::RawSplit);
        assert_eq!(plan.exact_size, 22);
        assert_eq!(
            plan.set_identity,
            "f756f6a81285a655f4cecfb2c6333e482e76dab24742de6ec73a15be3e665d47"
        );
        let mut missing = inputs;
        missing[1].relative_path = "movie.mkv.003";
        assert_eq!(plan_volumes(&missing), Err("archive_volume_gap"));
        assert_eq!(plan_volumes(&inputs[..1]), Err("archive_volume_gap"));
    }

    #[test]
    fn trusts_single_zip_evidence_and_rejects_unproven_split_hints() {
        let zip_identity = "1".repeat(64);
        let mut zip = vec![0_u8; 30];
        zip[..4].copy_from_slice(b"PK\x03\x04");
        let split_zip = [VolumeInput {
            content_identity: &zip_identity,
            relative_path: "release.z01",
            exact_size: 30,
            head: &zip,
            tail: b"",
        }];
        assert_eq!(
            plan_volumes(&split_zip)
                .expect("plan content-proven single ZIP")
                .kind,
            VolumePlanKind::SingleArchive(ArchiveFormat::Zip)
        );
        let mut spanned = b"PK\x07\x08".to_vec();
        spanned.extend_from_slice(&zip);
        let identities = ["4".repeat(64), "5".repeat(64), "6".repeat(64)];
        let real_split_zip = [
            VolumeInput {
                content_identity: &identities[0],
                relative_path: "release.z01",
                exact_size: 64 * 1024,
                head: &spanned,
                tail: b"first fragment",
            },
            VolumeInput {
                content_identity: &identities[1],
                relative_path: "release.z02",
                exact_size: 64 * 1024,
                head: b"middle fragment",
                tail: b"middle fragment",
            },
            VolumeInput {
                content_identity: &identities[2],
                relative_path: "release.zip",
                exact_size: 512,
                head: b"terminal fragment",
                tail: b"PK\x05\x06",
            },
        ];
        assert_eq!(
            plan_volumes(&real_split_zip),
            Err("archive_layout_unsupported")
        );

        let unproven_identity = "3".repeat(64);
        let unproven = [VolumeInput {
            content_identity: &unproven_identity,
            relative_path: "release.z01",
            exact_size: 7,
            head: b"not zip",
            tail: b"",
        }];
        assert_eq!(plan_volumes(&unproven), Err("archive_identity_unproven"));
    }
}
