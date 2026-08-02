use crc32fast::hash;
use serde::Serialize;
use unicode_normalization::UnicodeNormalization;

const MAX_HEADER_BYTES: usize = 2 * 1024 * 1024;
const MAX_ARCHIVE_PATH_BYTES: usize = 2_048;
const MAX_ARCHIVE_PATH_COMPONENTS: usize = 64;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ArchiveFormat {
    Rar4,
    Rar5,
    SevenZip,
    Zip,
    Gzip,
    Tar,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ArchiveEvidence {
    pub format: ArchiveFormat,
    pub header_bytes: usize,
    pub encrypted_header: bool,
    pub volume_layout: bool,
    pub first_volume: bool,
    pub volume_number: Option<u32>,
    pub logical_size: Option<u64>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct VolumeEndEvidence {
    pub next_volume: bool,
    pub volume_number: Option<u32>,
}

pub fn detect_archive(head: &[u8]) -> Result<Option<ArchiveEvidence>, &'static str> {
    if head.len() > MAX_HEADER_BYTES {
        return Err("archive_header_budget");
    }
    if head.starts_with(b"Rar!\x1a\x07\x00") {
        return detect_rar4(head).map(Some);
    }
    if head.starts_with(b"Rar!\x1a\x07\x01\x00") {
        return detect_rar5(head).map(Some);
    }
    if head.starts_with(b"\x37\x7a\xbc\xaf\x27\x1c") {
        return detect_7zip(head).map(Some);
    }
    if head.starts_with(b"PK\x03\x04") {
        return detect_zip(head).map(Some);
    }
    if head.starts_with(b"PK\x07\x08") {
        let mut evidence = detect_zip(head.get(4..).ok_or("archive_header_incomplete")?)?;
        evidence.header_bytes = evidence
            .header_bytes
            .checked_add(4)
            .ok_or("archive_header_invalid")?;
        evidence.volume_layout = true;
        evidence.first_volume = true;
        return Ok(Some(evidence));
    }
    if head.starts_with(b"\x1f\x8b") {
        return detect_gzip(head).map(Some);
    }
    if head.get(257..263) == Some(b"ustar\0") || head.get(257..263) == Some(b"ustar ") {
        return detect_tar(head).map(Some);
    }
    Ok(None)
}

fn detect_rar4(head: &[u8]) -> Result<ArchiveEvidence, &'static str> {
    let header = head.get(7..14).ok_or("archive_header_incomplete")?;
    if header[2] != 0x73 {
        return Err("archive_header_invalid");
    }
    let flags = u16::from_le_bytes(header[3..5].try_into().expect("RAR4 flags"));
    let header_size = usize::from(u16::from_le_bytes(
        header[5..7].try_into().expect("RAR4 header size"),
    ));
    let expected_size = if flags & 0x0200 != 0 { 14 } else { 13 };
    if flags & !0x03ff != 0
        || flags & 0x0100 != 0 && flags & 0x0001 == 0
        || flags & 0x0200 != 0 && flags & 0x0080 == 0
        || header_size != expected_size
    {
        return Err("archive_header_invalid");
    }
    if 7usize
        .checked_add(header_size)
        .is_none_or(|end| end > head.len())
    {
        return Err("archive_header_incomplete");
    }
    let end = 7 + header_size;
    let expected_crc = u16::from_le_bytes(header[..2].try_into().expect("RAR4 header CRC"));
    if crc32fast::hash(&head[9..end]) as u16 != expected_crc {
        return Err("archive_header_invalid");
    }
    Ok(ArchiveEvidence {
        format: ArchiveFormat::Rar4,
        header_bytes: end,
        encrypted_header: flags & 0x0080 != 0,
        volume_layout: flags & 0x0001 != 0,
        first_volume: flags & 0x0100 != 0,
        volume_number: None,
        logical_size: None,
    })
}

pub(crate) fn rar5_vint(data: &[u8], offset: usize) -> Result<(u64, usize), &'static str> {
    let mut value = 0_u64;
    for length in 1..=10 {
        let index = offset
            .checked_add(length - 1)
            .ok_or("archive_header_invalid")?;
        let byte = *data.get(index).ok_or("archive_header_incomplete")?;
        let payload = u64::from(byte & 0x7f);
        if length == 10 && payload > 1 {
            return Err("archive_header_invalid");
        }
        value = value
            .checked_add(payload << ((length - 1) * 7))
            .ok_or("archive_header_invalid")?;
        if byte & 0x80 == 0 {
            return Ok((value, length));
        }
    }
    Err("archive_header_invalid")
}

fn detect_rar5(head: &[u8]) -> Result<ArchiveEvidence, &'static str> {
    head.get(8..12).ok_or("archive_header_incomplete")?;
    let (header_size, size_bytes) = rar5_vint(head, 12)?;
    let header_size = usize::try_from(header_size).map_err(|_| "archive_header_invalid")?;
    if header_size == 0 {
        return Err("archive_header_invalid");
    }
    let content_start = 12usize
        .checked_add(size_bytes)
        .ok_or("archive_header_invalid")?;
    let end = content_start
        .checked_add(header_size)
        .ok_or("archive_header_invalid")?;
    let header = head
        .get(content_start..end)
        .ok_or("archive_header_incomplete")?;
    let expected_crc = u32::from_le_bytes(head[8..12].try_into().expect("RAR5 header CRC"));
    if hash(&head[12..end]) != expected_crc {
        return Err("archive_header_invalid");
    }
    let (header_type, type_bytes) = rar5_vint(header, 0)?;
    if header_type == 4 {
        return Ok(ArchiveEvidence {
            format: ArchiveFormat::Rar5,
            header_bytes: end,
            encrypted_header: true,
            volume_layout: true,
            first_volume: false,
            volume_number: None,
            logical_size: None,
        });
    }
    if header_type != 1 {
        return Err("archive_header_invalid");
    }
    let (header_flags, flag_bytes) = rar5_vint(header, type_bytes)?;
    if header_flags & !0x007f != 0 || header_flags & 0x0002 != 0 {
        return Err("archive_header_invalid");
    }
    let mut body_offset = type_bytes
        .checked_add(flag_bytes)
        .ok_or("archive_header_invalid")?;
    let extra_size = if header_flags & 0x0001 != 0 {
        let (size, bytes) = rar5_vint(header, body_offset)?;
        body_offset = body_offset
            .checked_add(bytes)
            .ok_or("archive_header_invalid")?;
        usize::try_from(size).map_err(|_| "archive_header_invalid")?
    } else {
        0
    };
    let body_end = header
        .len()
        .checked_sub(extra_size)
        .ok_or("archive_header_invalid")?;
    let (archive_flags, archive_flag_bytes) = rar5_vint(header, body_offset)?;
    if archive_flags & !0x001f != 0 || archive_flags & 0x0002 != 0 && archive_flags & 0x0001 == 0 {
        return Err("archive_header_invalid");
    }
    body_offset = body_offset
        .checked_add(archive_flag_bytes)
        .ok_or("archive_header_invalid")?;
    let volume_number = if archive_flags & 0x0002 != 0 {
        let (number, number_bytes) = rar5_vint(header, body_offset)?;
        body_offset = body_offset
            .checked_add(number_bytes)
            .ok_or("archive_header_invalid")?;
        let number = u32::try_from(number).map_err(|_| "archive_header_invalid")?;
        if number == 0 {
            return Err("archive_header_invalid");
        }
        Some(number)
    } else {
        None
    };
    if body_offset != body_end {
        return Err("archive_header_invalid");
    }
    Ok(ArchiveEvidence {
        format: ArchiveFormat::Rar5,
        header_bytes: end,
        encrypted_header: false,
        volume_layout: archive_flags & 0x0001 != 0,
        first_volume: archive_flags & 0x0001 != 0 && volume_number.is_none(),
        volume_number,
        logical_size: None,
    })
}

fn detect_7zip(head: &[u8]) -> Result<ArchiveEvidence, &'static str> {
    let header = head.get(..32).ok_or("archive_header_incomplete")?;
    if header[6] != 0 {
        return Err("archive_header_invalid");
    }
    let expected_crc = u32::from_le_bytes(header[8..12].try_into().expect("7-Zip header CRC"));
    if hash(&header[12..32]) != expected_crc {
        return Err("archive_header_invalid");
    }
    let next_header_offset =
        u64::from_le_bytes(header[12..20].try_into().expect("7-Zip next header offset"));
    let next_header_size =
        u64::from_le_bytes(header[20..28].try_into().expect("7-Zip next header size"));
    let logical_size = 32_u64
        .checked_add(next_header_offset)
        .and_then(|value| value.checked_add(next_header_size))
        .ok_or("archive_header_invalid")?;
    Ok(ArchiveEvidence {
        format: ArchiveFormat::SevenZip,
        header_bytes: 32,
        encrypted_header: false,
        volume_layout: false,
        first_volume: false,
        volume_number: None,
        logical_size: Some(logical_size),
    })
}

fn detect_zip(head: &[u8]) -> Result<ArchiveEvidence, &'static str> {
    let header = head.get(..30).ok_or("archive_header_incomplete")?;
    let flags = u16::from_le_bytes(header[6..8].try_into().expect("ZIP flags"));
    let name_bytes = usize::from(u16::from_le_bytes(
        header[26..28].try_into().expect("ZIP filename length"),
    ));
    let extra_bytes = usize::from(u16::from_le_bytes(
        header[28..30].try_into().expect("ZIP extra length"),
    ));
    let header_bytes = 30usize
        .checked_add(name_bytes)
        .and_then(|value| value.checked_add(extra_bytes))
        .ok_or("archive_header_invalid")?;
    if header_bytes > head.len() {
        return Err("archive_header_incomplete");
    }
    Ok(ArchiveEvidence {
        format: ArchiveFormat::Zip,
        header_bytes,
        encrypted_header: flags & 0x0001 != 0,
        volume_layout: false,
        first_volume: false,
        volume_number: None,
        logical_size: None,
    })
}

fn detect_gzip(head: &[u8]) -> Result<ArchiveEvidence, &'static str> {
    let header = head.get(..10).ok_or("archive_header_incomplete")?;
    if header[2] != 8 || header[3] & 0xe0 != 0 {
        return Err("archive_header_invalid");
    }
    Ok(ArchiveEvidence {
        format: ArchiveFormat::Gzip,
        header_bytes: 10,
        encrypted_header: false,
        volume_layout: false,
        first_volume: false,
        volume_number: None,
        logical_size: None,
    })
}

fn detect_tar(head: &[u8]) -> Result<ArchiveEvidence, &'static str> {
    let header = head.get(..512).ok_or("archive_header_incomplete")?;
    let checksum = header[148..156]
        .iter()
        .copied()
        .take_while(|byte| *byte != 0)
        .filter(|byte| *byte != b' ')
        .try_fold(0_u64, |value, byte| {
            if !(b'0'..=b'7').contains(&byte) {
                return Err("archive_header_invalid");
            }
            value
                .checked_mul(8)
                .and_then(|value| value.checked_add(u64::from(byte - b'0')))
                .ok_or("archive_header_invalid")
        })?;
    let actual = header[..148]
        .iter()
        .fold(0_u64, |total, byte| total + u64::from(*byte))
        + u64::from(b' ') * 8
        + header[156..]
            .iter()
            .fold(0_u64, |total, byte| total + u64::from(*byte));
    if checksum != actual {
        return Err("archive_header_invalid");
    }
    Ok(ArchiveEvidence {
        format: ArchiveFormat::Tar,
        header_bytes: 512,
        encrypted_header: false,
        volume_layout: false,
        first_volume: false,
        volume_number: None,
        logical_size: None,
    })
}

pub fn detect_volume_end(
    format: ArchiveFormat,
    tail: &[u8],
) -> Result<Option<VolumeEndEvidence>, &'static str> {
    if tail.len() > MAX_HEADER_BYTES {
        return Err("archive_header_budget");
    }
    match format {
        ArchiveFormat::Rar4 => detect_rar4_end(tail).map(Some),
        ArchiveFormat::Rar5 => detect_rar5_end(tail).map(Some),
        _ => Ok(None),
    }
}

fn detect_rar4_end(tail: &[u8]) -> Result<VolumeEndEvidence, &'static str> {
    let mut found = None;
    for offset in 0..tail.len().saturating_sub(6) {
        let header = &tail[offset..offset + 7];
        if header[2] != 0x7b {
            continue;
        }
        let flags = u16::from_le_bytes(header[3..5].try_into().expect("RAR4 end flags"));
        if flags & !0x000f != 0 {
            continue;
        }
        let header_size = usize::from(u16::from_le_bytes(
            header[5..7].try_into().expect("RAR4 end size"),
        ));
        let end = offset + header_size;
        if header_size < 7 || end > tail.len() {
            continue;
        }
        let expected_crc = u16::from_le_bytes(header[..2].try_into().expect("RAR4 end CRC"));
        if hash(&tail[offset + 2..end]) as u16 != expected_crc {
            continue;
        }
        let mut body_offset = offset + 7;
        if flags & 0x0002 != 0 {
            body_offset = body_offset.checked_add(4).ok_or("archive_header_invalid")?;
        }
        let volume_number = if flags & 0x0008 != 0 {
            let bytes = tail
                .get(body_offset..body_offset + 2)
                .ok_or("archive_header_invalid")?;
            body_offset += 2;
            Some(u32::from(u16::from_le_bytes(
                bytes.try_into().expect("RAR4 end volume number"),
            )))
        } else {
            None
        };
        if flags & 0x0004 != 0 {
            body_offset = body_offset.checked_add(7).ok_or("archive_header_invalid")?;
        }
        let next_volume = flags & 0x0001 != 0;
        if body_offset != end
            || !valid_rar_volume_padding(tail, end, next_volume)
            || found.is_some()
        {
            return Err("archive_header_invalid");
        }
        found = Some(VolumeEndEvidence {
            next_volume,
            volume_number,
        });
    }
    found.ok_or("archive_header_incomplete")
}

fn detect_rar5_end(tail: &[u8]) -> Result<VolumeEndEvidence, &'static str> {
    let mut found = None;
    for offset in 0..tail.len().saturating_sub(6) {
        let crc_bytes = &tail[offset..offset + 4];
        let Ok((header_size, size_bytes)) = rar5_vint(tail, offset + 4) else {
            continue;
        };
        let Ok(header_size) = usize::try_from(header_size) else {
            continue;
        };
        let content_start = offset + 4 + size_bytes;
        let Some(end) = content_start.checked_add(header_size) else {
            continue;
        };
        if header_size == 0 || end > tail.len() {
            continue;
        }
        let header = &tail[content_start..end];
        let expected_crc = u32::from_le_bytes(crc_bytes.try_into().expect("RAR5 end header CRC"));
        if hash(&tail[offset + 4..end]) != expected_crc {
            continue;
        }
        let Ok((header_type, type_bytes)) = rar5_vint(header, 0) else {
            continue;
        };
        let Ok((header_flags, flag_bytes)) = rar5_vint(header, type_bytes) else {
            continue;
        };
        // RAR's own ENDARC records carry HFL_SKIPIFUNKNOWN in archives
        // produced by current WinRAR. No other common-header flag is valid
        // for this record.
        if header_type != 5 || header_flags & !0x0004 != 0 {
            continue;
        }
        let body_offset = type_bytes
            .checked_add(flag_bytes)
            .ok_or("archive_header_invalid")?;
        let Ok((end_flags, end_flag_bytes)) = rar5_vint(header, body_offset) else {
            continue;
        };
        let next_volume = end_flags & 0x0001 != 0;
        if end_flags & !0x0001 != 0
            || body_offset.checked_add(end_flag_bytes) != Some(header.len())
            || !valid_rar_volume_padding(tail, end, next_volume)
            || found.is_some()
        {
            return Err("archive_header_invalid");
        }
        found = Some(VolumeEndEvidence {
            next_volume,
            volume_number: None,
        });
    }
    found.ok_or("archive_header_incomplete")
}

fn valid_rar_volume_padding(tail: &[u8], end: usize, next_volume: bool) -> bool {
    end == tail.len() || next_volume && tail[end..].iter().all(|byte| *byte == 0)
}

fn unsafe_unicode(character: char) -> bool {
    character.is_control()
        || matches!(
            character,
            '\u{200b}'..='\u{200f}'
                | '\u{202a}'..='\u{202e}'
                | '\u{2060}'..='\u{206f}'
                | '\u{feff}'
        )
}

fn windows_reserved(component: &str) -> bool {
    let stem = component
        .split_once('.')
        .map_or(component, |(stem, _)| stem);
    stem.eq_ignore_ascii_case("con")
        || stem.eq_ignore_ascii_case("prn")
        || stem.eq_ignore_ascii_case("aux")
        || stem.eq_ignore_ascii_case("nul")
        || stem.len() == 4
            && stem.is_char_boundary(3)
            && (stem[..3].eq_ignore_ascii_case("com") || stem[..3].eq_ignore_ascii_case("lpt"))
            && (b'1'..=b'9').contains(&stem.as_bytes()[3])
}

pub fn normalize_archive_path(raw: &str) -> Result<String, &'static str> {
    if raw.is_empty()
        || raw.len() > MAX_ARCHIVE_PATH_BYTES
        || raw.chars().any(unsafe_unicode)
        || !raw.nfc().eq(raw.chars())
    {
        return Err("archive_path_invalid");
    }
    let normalized = raw.replace('\\', "/");
    if normalized.starts_with('/')
        || normalized.ends_with('/')
        || normalized
            .as_bytes()
            .get(1)
            .is_some_and(|byte| *byte == b':')
    {
        return Err("archive_path_invalid");
    }
    let mut component_count = 0;
    for component in normalized.split('/') {
        component_count += 1;
        if component_count > MAX_ARCHIVE_PATH_COMPONENTS
            || component.is_empty()
            || matches!(component, "." | "..")
            || component.len() > 255
            || component.ends_with([' ', '.'])
            || component.contains(':')
            || windows_reserved(component)
        {
            return Err("archive_path_invalid");
        }
    }
    Ok(normalized)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum VolumeScheme {
    RarPart,
    RarLegacy,
    SevenZipSplit,
    ZipSplit,
    NumericSplit,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct VolumeHint {
    pub base: String,
    pub number: u32,
    pub scheme: VolumeScheme,
}

fn decimal(value: &str) -> Option<u32> {
    if value.is_empty() || value.bytes().any(|byte| !byte.is_ascii_digit()) {
        return None;
    }
    value.parse().ok()
}

pub fn classify_volume_name(name: &str) -> Option<VolumeHint> {
    let name = normalize_archive_path(name).ok()?;
    let lower = name.to_ascii_lowercase();
    if let Some(prefix) = lower.strip_suffix(".rar") {
        if let Some((base, number)) = prefix.rsplit_once(".part") {
            return Some(VolumeHint {
                base: base.to_owned(),
                number: decimal(number)?,
                scheme: VolumeScheme::RarPart,
            });
        }
        return Some(VolumeHint {
            base: prefix.to_owned(),
            number: 0,
            scheme: VolumeScheme::RarLegacy,
        });
    }
    if let Some((base, suffix)) = lower.rsplit_once(".r")
        && suffix.len() == 2
    {
        return Some(VolumeHint {
            base: base.to_owned(),
            number: decimal(suffix)?.checked_add(1)?,
            scheme: VolumeScheme::RarLegacy,
        });
    }
    if let Some((base, suffix)) = lower.rsplit_once(".7z.") {
        return Some(VolumeHint {
            base: base.to_owned(),
            number: decimal(suffix)?,
            scheme: VolumeScheme::SevenZipSplit,
        });
    }
    if let Some((base, suffix)) = lower.rsplit_once(".z")
        && suffix.len() == 2
    {
        return Some(VolumeHint {
            base: base.to_owned(),
            number: decimal(suffix)?,
            scheme: VolumeScheme::ZipSplit,
        });
    }
    if let Some((base, suffix)) = lower.rsplit_once('.') {
        return Some(VolumeHint {
            base: base.to_owned(),
            number: decimal(suffix)?,
            scheme: VolumeScheme::NumericSplit,
        });
    }
    None
}

#[cfg(test)]
mod tests {
    use super::{
        ArchiveFormat, VolumeHint, VolumeScheme, classify_volume_name, detect_archive,
        detect_volume_end, normalize_archive_path, rar5_vint,
    };

    #[test]
    fn detects_validated_magic_before_filename_layout() {
        let mut seven_zip = b"\x37\x7a\xbc\xaf\x27\x1c\0\x04\0\0\0\0".to_vec();
        seven_zip.extend_from_slice(&[0; 20]);
        let crc = crc32fast::hash(&seven_zip[12..32]);
        seven_zip[8..12].copy_from_slice(&crc.to_le_bytes());
        assert_eq!(
            detect_archive(&seven_zip).unwrap().unwrap().format,
            ArchiveFormat::SevenZip
        );

        let mut zip = b"PK\x03\x04".to_vec();
        zip.extend_from_slice(&[0; 22]);
        zip.extend_from_slice(&4_u16.to_le_bytes());
        zip.extend_from_slice(&0_u16.to_le_bytes());
        zip.extend_from_slice(b"name");
        assert_eq!(
            detect_archive(&zip).unwrap().unwrap().format,
            ArchiveFormat::Zip
        );
        let mut spanned_zip = b"PK\x07\x08".to_vec();
        spanned_zip.extend_from_slice(&zip);
        let spanned = detect_archive(&spanned_zip).unwrap().unwrap();
        assert_eq!(spanned.format, ArchiveFormat::Zip);
        assert!(spanned.volume_layout);
        assert!(spanned.first_volume);
        assert_eq!(spanned.header_bytes, zip.len() + 4);
        assert_eq!(detect_archive(b"not an archive").unwrap(), None);

        let mut tar = vec![0_u8; 512];
        tar[257..263].copy_from_slice(b"ustar\0");
        tar[148..156].fill(b' ');
        let checksum = tar.iter().map(|byte| u64::from(*byte)).sum::<u64>();
        let encoded = format!("{checksum:06o}\0 ");
        tar[148..156].copy_from_slice(encoded.as_bytes());
        assert_eq!(
            detect_archive(&tar).unwrap().unwrap().format,
            ArchiveFormat::Tar
        );
    }

    #[test]
    fn detects_encryption_layout_and_truncated_or_corrupt_headers() {
        let mut maximum_vint = vec![0xff; 9];
        maximum_vint.push(1);
        assert_eq!(rar5_vint(&maximum_vint, 0), Ok((u64::MAX, 10)));
        maximum_vint[9] = 2;
        assert_eq!(rar5_vint(&maximum_vint, 0), Err("archive_header_invalid"));

        let mut rar4 = b"Rar!\x1a\x07\x00".to_vec();
        rar4.extend_from_slice(&[0, 0, 0x73, 0x81, 0, 13, 0]);
        rar4.extend_from_slice(&[0; 6]);
        let crc = crc32fast::hash(&rar4[9..20]) as u16;
        rar4[7..9].copy_from_slice(&crc.to_le_bytes());
        let evidence = detect_archive(&rar4).unwrap().unwrap();
        assert!(evidence.encrypted_header);
        assert!(evidence.volume_layout);
        let mut malformed = rar4.clone();
        malformed[12..14].copy_from_slice(&7_u16.to_le_bytes());
        let crc = crc32fast::hash(&malformed[9..14]) as u16;
        malformed[7..9].copy_from_slice(&crc.to_le_bytes());
        assert_eq!(detect_archive(&malformed), Err("archive_header_invalid"));
        assert_eq!(
            detect_archive(b"Rar!\x1a\x07\x00"),
            Err("archive_header_incomplete")
        );

        let mut rar5 = b"Rar!\x1a\x07\x01\x00".to_vec();
        rar5.extend_from_slice(&[0; 4]);
        rar5.extend_from_slice(&[3, 1, 0, 1]);
        let crc = crc32fast::hash(&rar5[12..]);
        rar5[8..12].copy_from_slice(&crc.to_le_bytes());
        let evidence = detect_archive(&rar5).unwrap().unwrap();
        assert!(evidence.volume_layout);
        assert!(evidence.first_volume);
        assert_eq!(evidence.volume_number, None);

        let encrypted_rar5 = [
            0x52, 0x61, 0x72, 0x21, 0x1a, 0x07, 0x01, 0x00, 0xed, 0xb7, 0x6b, 0x70, 0x21, 0x04,
            0x00, 0x00, 0x01, 0x0f, 0x19, 0xa4, 0xbd, 0xea, 0xc6, 0x30, 0xfe, 0x95, 0x26, 0x06,
            0x44, 0x8c, 0xd5, 0x2c, 0x84, 0xb1, 0x4e, 0x70, 0x48, 0xa0, 0xbc, 0x26, 0x08, 0xae,
            0xf8, 0xd7, 0xe8, 0x6c,
        ];
        let evidence = detect_archive(&encrypted_rar5).unwrap().unwrap();
        assert_eq!(evidence.format, ArchiveFormat::Rar5);
        assert!(evidence.encrypted_header);
        assert!(evidence.volume_layout);

        for archive_body in [[2, 1], [3, 0]] {
            let mut invalid = b"Rar!\x1a\x07\x01\0".to_vec();
            invalid.extend_from_slice(&[0; 4]);
            invalid.extend_from_slice(&[4, 1, 0]);
            invalid.extend_from_slice(&archive_body);
            let crc = crc32fast::hash(&invalid[12..]);
            invalid[8..12].copy_from_slice(&crc.to_le_bytes());
            assert_eq!(detect_archive(&invalid), Err("archive_header_invalid"));
        }

        let mut corrupt_7z = b"\x37\x7a\xbc\xaf\x27\x1c\0\x04".to_vec();
        corrupt_7z.extend_from_slice(&[0; 24]);
        assert_eq!(detect_archive(&corrupt_7z), Err("archive_header_invalid"));

        let mut encrypted_zip = vec![0_u8; 30];
        encrypted_zip[..4].copy_from_slice(b"PK\x03\x04");
        encrypted_zip[6..8].copy_from_slice(&1_u16.to_le_bytes());
        encrypted_zip[26..28].copy_from_slice(&4_u16.to_le_bytes());
        encrypted_zip.extend_from_slice(b"name");
        assert!(
            detect_archive(&encrypted_zip)
                .unwrap()
                .unwrap()
                .encrypted_header
        );
    }

    #[test]
    fn validates_rar_terminal_headers_and_only_intermediate_zero_padding() {
        fn rar5_end(next_volume: bool) -> Vec<u8> {
            let mut header = vec![0; 4];
            header.extend_from_slice(&[3, 5, 4, u8::from(next_volume)]);
            let crc = crc32fast::hash(&header[4..]);
            header[..4].copy_from_slice(&crc.to_le_bytes());
            header
        }

        let mut intermediate = rar5_end(true);
        intermediate.extend_from_slice(&[0; 8]);
        assert!(
            detect_volume_end(ArchiveFormat::Rar5, &intermediate)
                .unwrap()
                .unwrap()
                .next_volume
        );
        let final_volume = rar5_end(false);
        assert!(
            !detect_volume_end(ArchiveFormat::Rar5, &final_volume)
                .unwrap()
                .unwrap()
                .next_volume
        );

        let mut nonzero_trailer = rar5_end(true);
        nonzero_trailer.extend_from_slice(&[0, 1]);
        assert_eq!(
            detect_volume_end(ArchiveFormat::Rar5, &nonzero_trailer),
            Err("archive_header_invalid")
        );
        let mut padded_final = rar5_end(false);
        padded_final.push(0);
        assert_eq!(
            detect_volume_end(ArchiveFormat::Rar5, &padded_final),
            Err("archive_header_invalid")
        );
    }

    #[test]
    fn confines_and_normalizes_archive_entry_paths() {
        assert_eq!(
            normalize_archive_path(r"Season 01\Episode.mkv"),
            Ok("Season 01/Episode.mkv".to_owned())
        );
        for invalid in [
            "/absolute.mkv",
            r"C:\drive.mkv",
            "../escape.mkv",
            "nested/../../escape.mkv",
            "CON.txt",
            "file.mkv.",
            "safe/\u{202e}evil.mkv",
            "decomposed-e\u{301}.mkv",
        ] {
            assert_eq!(
                normalize_archive_path(invalid),
                Err("archive_path_invalid"),
                "{invalid:?}"
            );
        }
    }

    #[test]
    fn classifies_common_volume_names_without_treating_them_as_proof() {
        assert_eq!(
            classify_volume_name("release.part03.rar"),
            Some(VolumeHint {
                base: "release".into(),
                number: 3,
                scheme: VolumeScheme::RarPart,
            })
        );
        assert_eq!(classify_volume_name("release.r00").unwrap().number, 1);
        assert_eq!(
            classify_volume_name("release.7z.002").unwrap().scheme,
            VolumeScheme::SevenZipSplit
        );
        assert_eq!(
            classify_volume_name("obfuscated.001").unwrap().scheme,
            VolumeScheme::NumericSplit
        );
        assert_eq!(classify_volume_name("obfuscated.0001").unwrap().number, 1);
        assert!(classify_volume_name("../release.part01.rar").is_none());
        assert!(classify_volume_name("movie.mkv").is_none());
    }
}
