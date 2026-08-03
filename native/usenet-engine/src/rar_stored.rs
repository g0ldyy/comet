use crate::archive::{normalize_archive_path, rar5_vint};
use std::collections::BTreeSet;

const RAR5_MAGIC: &[u8; 8] = b"Rar!\x1a\x07\x01\0";
const RAR4_MAGIC: &[u8; 7] = b"Rar!\x1a\x07\0";
const MAX_MEMBERS: usize = crate::nzb::MAX_FILES;
const MAX_RANGES: usize = crate::nzb::MAX_FILES;
use crate::limits::MAX_LOGICAL_BYTES;
const MAX_HEADER_READ_BYTES: usize = 64 * 1024 * 1024;
const MAX_BLOCK_HEADER_BYTES: usize = 2 * 1024 * 1024;
const RAR5_HEADER_SKIP_IF_UNKNOWN: u64 = 0x0004;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StoredRange {
    pub volume_index: usize,
    pub offset: u64,
    pub length: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StoredMember {
    pub relative_path: String,
    pub exact_size: u64,
    pub ranges: Vec<StoredRange>,
}

struct Block {
    kind: u64,
    flags: u64,
    header: Vec<u8>,
    body_start: usize,
    body_end: usize,
    data_offset: u64,
    data_size: u64,
    end: u64,
}

impl Block {
    fn body(&self) -> &[u8] {
        &self.header[self.body_start..self.body_end]
    }

    fn extra(&self) -> &[u8] {
        &self.header[self.body_end..]
    }
}

struct PendingMember {
    relative_path: String,
    exact_size: u64,
    fragment_bytes: u64,
    ranges: Vec<StoredRange>,
}

struct MemberCollector {
    members: Vec<StoredMember>,
    paths: BTreeSet<String>,
    pending: Option<PendingMember>,
    range_count: usize,
    logical_bytes: u64,
}

struct MemberPart {
    relative_path: String,
    exact_size: u64,
    fragment_size: u64,
    split_before: bool,
    split_after: bool,
    volume_index: usize,
    offset: u64,
}

struct ParsedFile {
    relative_path: String,
    exact_size: u64,
    fragment_size: u64,
    split_before: bool,
    split_after: bool,
}

impl MemberCollector {
    fn new() -> Self {
        Self {
            members: Vec::new(),
            paths: BTreeSet::new(),
            pending: None,
            range_count: 0,
            logical_bytes: 0,
        }
    }

    fn push(&mut self, part: MemberPart) -> Result<(), &'static str> {
        self.range_count += 1;
        if self.range_count > MAX_RANGES {
            return Err("archive_volume_budget");
        }
        let range = StoredRange {
            volume_index: part.volume_index,
            offset: part.offset,
            length: part.fragment_size,
        };
        if part.split_before {
            let current = self
                .pending
                .as_mut()
                .filter(|member| {
                    member.relative_path == part.relative_path
                        && member.exact_size == part.exact_size
                })
                .ok_or("archive_volume_conflict")?;
            if current
                .ranges
                .last()
                .is_none_or(|range| range.volume_index + 1 != part.volume_index)
            {
                return Err("archive_volume_conflict");
            }
            current.fragment_bytes = current
                .fragment_bytes
                .checked_add(part.fragment_size)
                .ok_or("archive_volume_budget")?;
            current.ranges.push(range);
        } else {
            if self.pending.is_some() {
                return Err("archive_volume_conflict");
            }
            self.pending = Some(PendingMember {
                relative_path: part.relative_path,
                exact_size: part.exact_size,
                fragment_bytes: part.fragment_size,
                ranges: vec![range],
            });
        }
        if !part.split_after {
            let completed = self.pending.take().ok_or("archive_volume_conflict")?;
            if completed.fragment_bytes != completed.exact_size {
                return Err("archive_volume_conflict");
            }
            if self.members.len() >= MAX_MEMBERS {
                return Err("archive_volume_budget");
            }
            if !self.paths.insert(completed.relative_path.to_lowercase()) {
                return Err("archive_volume_conflict");
            }
            self.logical_bytes = self
                .logical_bytes
                .checked_add(completed.exact_size)
                .filter(|total| *total <= MAX_LOGICAL_BYTES)
                .ok_or("archive_volume_budget")?;
            self.members.push(StoredMember {
                relative_path: completed.relative_path,
                exact_size: completed.exact_size,
                ranges: completed.ranges,
            });
        }
        Ok(())
    }

    fn finish(self) -> Result<Vec<StoredMember>, &'static str> {
        if self.pending.is_some() || self.members.is_empty() {
            return Err("archive_volume_conflict");
        }
        Ok(self.members)
    }
}

fn parse_block<F>(
    volume_index: usize,
    volume_size: u64,
    offset: u64,
    read_range: &mut F,
    header_read_bytes: &mut usize,
) -> Result<Block, &'static str>
where
    F: FnMut(usize, u64, usize) -> Result<Vec<u8>, &'static str>,
{
    let remaining = volume_size - offset;
    let prefix_size = remaining.min(14) as usize;
    if prefix_size < 5 {
        return Err("archive_header_incomplete");
    }
    let prefix = bounded_read(
        read_range,
        header_read_bytes,
        volume_index,
        offset,
        prefix_size,
    )?;
    if prefix.len() != prefix_size {
        return Err("archive_header_incomplete");
    }
    let expected_crc = u32::from_le_bytes(prefix[..4].try_into().expect("RAR5 block CRC field"));
    let (header_size, size_bytes) = rar5_vint(&prefix, 4)?;
    let header_size = usize::try_from(header_size).map_err(|_| "archive_header_invalid")?;
    if header_size == 0 || header_size > MAX_BLOCK_HEADER_BYTES {
        return Err("archive_header_invalid");
    }
    let encoded_header_size = 4 + size_bytes + header_size;
    let encoded_header = bounded_read(
        read_range,
        header_read_bytes,
        volume_index,
        offset,
        encoded_header_size,
    )?;
    if encoded_header.len() != encoded_header_size {
        return Err("archive_header_incomplete");
    }
    if crc32fast::hash(&encoded_header[4..]) != expected_crc {
        return Err("archive_header_invalid");
    }
    let header_start = 4 + size_bytes;
    let header = &encoded_header[header_start..];
    let (kind, kind_bytes) = rar5_vint(header, 0)?;
    let (flags, flag_bytes) = rar5_vint(header, kind_bytes)?;
    if flags & !0x003f != 0 {
        return Err("archive_header_invalid");
    }
    let mut cursor = kind_bytes + flag_bytes;
    let extra_size = if flags & 0x0001 != 0 {
        let (value, bytes) = rar5_vint(header, cursor)?;
        cursor += bytes;
        usize::try_from(value).map_err(|_| "archive_header_invalid")?
    } else {
        0
    };
    let data_size = if flags & 0x0002 != 0 {
        let (value, bytes) = rar5_vint(header, cursor)?;
        cursor += bytes;
        value
    } else {
        0
    };
    let body_end = header
        .len()
        .checked_sub(extra_size)
        .ok_or("archive_header_invalid")?;
    if cursor > body_end {
        return Err("archive_header_invalid");
    }
    let data_offset = offset
        .checked_add(encoded_header_size as u64)
        .ok_or("archive_header_invalid")?;
    let end = data_offset
        .checked_add(data_size)
        .ok_or("archive_header_invalid")?;
    if end > volume_size {
        return Err("archive_header_incomplete");
    }
    Ok(Block {
        kind,
        flags,
        header: encoded_header,
        body_start: header_start + cursor,
        body_end: header_start + body_end,
        data_offset,
        data_size,
        end,
    })
}

fn bounded_read<F>(
    read_range: &mut F,
    header_read_bytes: &mut usize,
    volume_index: usize,
    offset: u64,
    length: usize,
) -> Result<Vec<u8>, &'static str>
where
    F: FnMut(usize, u64, usize) -> Result<Vec<u8>, &'static str>,
{
    *header_read_bytes = header_read_bytes
        .checked_add(length)
        .filter(|total| *total <= MAX_HEADER_READ_BYTES)
        .ok_or("archive_volume_budget")?;
    read_range(volume_index, offset, length)
}

fn validate_volume_padding<F>(
    read_range: &mut F,
    header_read_bytes: &mut usize,
    volume_index: usize,
    volume_count: usize,
    volume_size: u64,
    offset: u64,
) -> Result<(), &'static str>
where
    F: FnMut(usize, u64, usize) -> Result<Vec<u8>, &'static str>,
{
    if volume_index + 1 == volume_count {
        return Err("archive_header_invalid");
    }
    let mut cursor = offset;
    while cursor < volume_size {
        let length = usize::try_from((volume_size - cursor).min(64 * 1024))
            .map_err(|_| "archive_volume_budget")?;
        if bounded_read(read_range, header_read_bytes, volume_index, cursor, length)?
            .iter()
            .any(|byte| *byte != 0)
        {
            return Err("archive_header_invalid");
        }
        cursor += length as u64;
    }
    Ok(())
}

fn validate_magic<F>(
    read_range: &mut F,
    header_read_bytes: &mut usize,
    volume_index: usize,
    volume_size: u64,
    magic: &[u8],
) -> Result<(), &'static str>
where
    F: FnMut(usize, u64, usize) -> Result<Vec<u8>, &'static str>,
{
    if volume_size < magic.len() as u64
        || bounded_read(read_range, header_read_bytes, volume_index, 0, magic.len())? != magic
    {
        return Err("archive_header_invalid");
    }
    Ok(())
}

fn parse_main(block: &Block, volume_index: usize, volumes: usize) -> Result<(), &'static str> {
    if block.flags & !(0x0001 | RAR5_HEADER_SKIP_IF_UNKNOWN) != 0 || block.data_size != 0 {
        return Err("archive_header_invalid");
    }
    let body = block.body();
    let (archive_flags, flag_bytes) = rar5_vint(body, 0)?;
    if archive_flags & !0x001f != 0 || archive_flags & 0x0004 != 0 {
        return Err("archive_direct_unsupported");
    }
    let volume = archive_flags & 0x0001 != 0;
    let numbered = archive_flags & 0x0002 != 0;
    if volume != (volumes > 1) {
        return Err("archive_volume_conflict");
    }
    let mut cursor = flag_bytes;
    if numbered {
        let (number, bytes) = rar5_vint(body, cursor)?;
        cursor += bytes;
        if usize::try_from(number).ok() != Some(volume_index) {
            return Err("archive_volume_conflict");
        }
    }
    if cursor != body.len() {
        return Err("archive_header_invalid");
    }
    parse_extras(block.extra(), false)
}

fn parse_extras(extra: &[u8], reject_file_features: bool) -> Result<(), &'static str> {
    let mut cursor = 0;
    while cursor < extra.len() {
        let (record_size, size_bytes) = rar5_vint(extra, cursor)?;
        cursor += size_bytes;
        let record_size = usize::try_from(record_size).map_err(|_| "archive_header_invalid")?;
        let end = cursor
            .checked_add(record_size)
            .ok_or("archive_header_invalid")?;
        let record = extra.get(cursor..end).ok_or("archive_header_invalid")?;
        let (record_type, _) = rar5_vint(record, 0)?;
        if reject_file_features && matches!(record_type, 0x01 | 0x05) {
            return Err("archive_direct_unsupported");
        }
        cursor = end;
    }
    Ok(())
}

fn parse_file(block: &Block) -> Result<ParsedFile, &'static str> {
    if block.flags & 0x0002 == 0 || block.flags & 0x0020 != 0 {
        return Err("archive_direct_unsupported");
    }
    let body = block.body();
    let mut cursor = 0;
    let (file_flags, bytes) = rar5_vint(body, cursor)?;
    cursor += bytes;
    if file_flags & !0x000f != 0 || file_flags & 0x0009 != 0 {
        return Err("archive_direct_unsupported");
    }
    let (unpacked_size, bytes) = rar5_vint(body, cursor)?;
    cursor += bytes;
    if unpacked_size == 0
        || unpacked_size > MAX_LOGICAL_BYTES
        || block.data_size == 0
        || block.data_size > unpacked_size
    {
        return Err("archive_direct_unsupported");
    }
    let (_attributes, bytes) = rar5_vint(body, cursor)?;
    cursor += bytes;
    if file_flags & 0x0002 != 0 {
        cursor += 4;
        body.get(..cursor).ok_or("archive_header_incomplete")?;
    }
    if file_flags & 0x0004 != 0 {
        cursor += 4;
        body.get(..cursor).ok_or("archive_header_incomplete")?;
    }
    let (compression, bytes) = rar5_vint(body, cursor)?;
    cursor += bytes;
    let version = compression & 0x3f;
    if version > 1
        || compression & 0x40 != 0
        || compression & 0x0380 != 0
        || compression & !0x3fff != 0
    {
        return Err("archive_direct_unsupported");
    }
    let (_host_os, bytes) = rar5_vint(body, cursor)?;
    cursor += bytes;
    let (name_size, bytes) = rar5_vint(body, cursor)?;
    cursor += bytes;
    let name_size = usize::try_from(name_size).map_err(|_| "archive_header_invalid")?;
    let name_end = cursor
        .checked_add(name_size)
        .ok_or("archive_header_invalid")?;
    let raw_name = std::str::from_utf8(
        body.get(cursor..name_end)
            .ok_or("archive_header_incomplete")?,
    )
    .map_err(|_| "archive_path_invalid")?;
    if name_end != body.len() {
        return Err("archive_path_invalid");
    }
    parse_extras(block.extra(), true)?;
    let relative_path = normalize_archive_path(raw_name)?;
    let split_before = block.flags & 0x0008 != 0;
    let split_after = block.flags & 0x0010 != 0;
    Ok(ParsedFile {
        relative_path,
        exact_size: unpacked_size,
        fragment_size: block.data_size,
        split_before,
        split_after,
    })
}

pub fn parse_rar5_stored_members<F>(
    volume_sizes: &[u64],
    mut read_range: F,
) -> Result<Vec<StoredMember>, &'static str>
where
    F: FnMut(usize, u64, usize) -> Result<Vec<u8>, &'static str>,
{
    let mut collector = MemberCollector::new();
    let mut header_read_bytes = 0usize;
    for (volume_index, volume_size) in volume_sizes.iter().copied().enumerate() {
        validate_magic(
            &mut read_range,
            &mut header_read_bytes,
            volume_index,
            volume_size,
            RAR5_MAGIC,
        )?;
        let mut offset = RAR5_MAGIC.len() as u64;
        let mut main_seen = false;
        let mut end_seen = false;
        let mut continuation_seen = false;
        while offset < volume_size {
            if end_seen {
                validate_volume_padding(
                    &mut read_range,
                    &mut header_read_bytes,
                    volume_index,
                    volume_sizes.len(),
                    volume_size,
                    offset,
                )?;
                break;
            }
            let block = parse_block(
                volume_index,
                volume_size,
                offset,
                &mut read_range,
                &mut header_read_bytes,
            )?;
            match block.kind {
                1 if !main_seen && offset == RAR5_MAGIC.len() as u64 => {
                    parse_main(&block, volume_index, volume_sizes.len())?;
                    main_seen = true;
                }
                2 if main_seen => {
                    let parsed = parse_file(&block)?;
                    let split_after = parsed.split_after;
                    collector.push(MemberPart {
                        relative_path: parsed.relative_path,
                        exact_size: parsed.exact_size,
                        fragment_size: parsed.fragment_size,
                        split_before: parsed.split_before,
                        split_after: parsed.split_after,
                        volume_index,
                        offset: block.data_offset,
                    })?;
                    if split_after {
                        // The next numbered volume proves continuation. Bytes
                        // outside the stored member range are never served.
                        continuation_seen = true;
                        break;
                    }
                }
                3 if main_seen => {
                    if block.flags & (0x0008 | 0x0010 | 0x0020) != 0 {
                        return Err("archive_direct_unsupported");
                    }
                    parse_extras(block.extra(), true)?;
                }
                4 => return Err("archive_direct_unsupported"),
                5 if main_seen => {
                    let body = block.body();
                    let (end_flags, bytes) = rar5_vint(body, 0)?;
                    if block.flags & !RAR5_HEADER_SKIP_IF_UNKNOWN != 0
                        || !block.extra().is_empty()
                        || bytes != body.len()
                        || end_flags & !0x0001 != 0
                        || (end_flags & 0x0001 != 0) != (volume_index + 1 < volume_sizes.len())
                        || block.data_size != 0
                    {
                        return Err("archive_volume_conflict");
                    }
                    end_seen = true;
                }
                _ => return Err("archive_header_invalid"),
            }
            offset = block.end;
        }
        if !main_seen || (!end_seen && !continuation_seen) {
            return Err("archive_header_incomplete");
        }
    }
    collector.finish()
}

pub fn parse_rar4_stored_members<F>(
    volume_sizes: &[u64],
    mut read_range: F,
) -> Result<Vec<StoredMember>, &'static str>
where
    F: FnMut(usize, u64, usize) -> Result<Vec<u8>, &'static str>,
{
    let mut collector = MemberCollector::new();
    let mut header_read_bytes = 0usize;
    for (volume_index, volume_size) in volume_sizes.iter().copied().enumerate() {
        validate_magic(
            &mut read_range,
            &mut header_read_bytes,
            volume_index,
            volume_size,
            RAR4_MAGIC,
        )?;
        let mut offset = RAR4_MAGIC.len() as u64;
        let mut main_seen = false;
        let mut end_seen = false;
        let mut continuation_seen = false;
        while offset < volume_size {
            if end_seen {
                validate_volume_padding(
                    &mut read_range,
                    &mut header_read_bytes,
                    volume_index,
                    volume_sizes.len(),
                    volume_size,
                    offset,
                )?;
                break;
            }
            let prefix = bounded_read(
                &mut read_range,
                &mut header_read_bytes,
                volume_index,
                offset,
                7,
            )?;
            if prefix.len() != 7 {
                return Err("archive_header_incomplete");
            }
            let header_size = usize::from(u16::from_le_bytes(
                prefix[5..7].try_into().expect("RAR4 header size field"),
            ));
            if !(7..=MAX_BLOCK_HEADER_BYTES).contains(&header_size)
                || offset
                    .checked_add(header_size as u64)
                    .is_none_or(|end| end > volume_size)
            {
                return Err("archive_header_incomplete");
            }
            let header = bounded_read(
                &mut read_range,
                &mut header_read_bytes,
                volume_index,
                offset,
                header_size,
            )?;
            if header.len() != header_size
                || crc32fast::hash(&header[2..]) as u16
                    != u16::from_le_bytes(header[..2].try_into().expect("RAR4 header CRC field"))
            {
                return Err("archive_header_invalid");
            }
            let kind = header[2];
            let flags =
                u16::from_le_bytes(header[3..5].try_into().expect("RAR4 header flags field"));
            let mut data_size = if flags & 0x8000 != 0 {
                u64::from(read_u32(&header, 7)?)
            } else {
                0
            };
            match kind {
                0x73 if !main_seen && offset == RAR4_MAGIC.len() as u64 => {
                    if header_size != 13
                        || flags & !0x01ff != 0
                        || flags & (0x0002 | 0x0008 | 0x0020 | 0x0040 | 0x0080) != 0
                        || (flags & 0x0001 != 0) != (volume_sizes.len() > 1)
                        || flags & 0x0100 != 0 && volume_index != 0
                    {
                        return Err("archive_direct_unsupported");
                    }
                    data_size = 0;
                    main_seen = true;
                }
                0x74 if main_seen => {
                    let parsed = parse_rar4_file(&header, flags)?;
                    data_size = parsed.fragment_size;
                    let split_after = parsed.split_after;
                    collector.push(MemberPart {
                        relative_path: parsed.relative_path,
                        exact_size: parsed.exact_size,
                        fragment_size: parsed.fragment_size,
                        split_before: parsed.split_before,
                        split_after: parsed.split_after,
                        volume_index,
                        offset: offset
                            .checked_add(header_size as u64)
                            .ok_or("archive_header_invalid")?,
                    })?;
                    // The next numbered volume proves continuation. Bytes
                    // outside the stored member range are never served.
                    continuation_seen = split_after;
                }
                0x7b if main_seen => {
                    if flags & !0x400f != 0 {
                        return Err("archive_header_invalid");
                    }
                    let mut cursor = 7usize;
                    if flags & 0x0002 != 0 {
                        cursor += 4;
                    }
                    if flags & 0x0008 != 0 {
                        if usize::from(read_u16(&header, cursor)?) != volume_index {
                            return Err("archive_volume_conflict");
                        }
                        cursor += 2;
                    }
                    if flags & 0x0004 != 0 {
                        cursor += 7;
                    }
                    if cursor != header_size
                        || (flags & 0x0001 != 0) != (volume_index + 1 < volume_sizes.len())
                    {
                        return Err("archive_volume_conflict");
                    }
                    data_size = 0;
                    end_seen = true;
                }
                0x72 => return Err("archive_header_invalid"),
                0x75..=0x7a if main_seen => {}
                _ if flags & 0x4000 != 0 => {}
                _ => return Err("archive_header_invalid"),
            }
            offset = offset
                .checked_add(header_size as u64)
                .and_then(|end| end.checked_add(data_size))
                .filter(|end| *end <= volume_size)
                .ok_or("archive_header_incomplete")?;
            if continuation_seen {
                break;
            }
        }
        if !main_seen || (!end_seen && !continuation_seen) {
            return Err("archive_header_incomplete");
        }
    }
    collector.finish()
}

fn parse_rar4_file(header: &[u8], flags: u16) -> Result<ParsedFile, &'static str> {
    if header.len() < 32
        || flags & 0x8000 == 0
        || flags & !0xdfff != 0
        || flags & (0x0004 | 0x0008 | 0x0010 | 0x0400 | 0x0800) != 0
        || flags & 0x00e0 == 0x00e0
    {
        return Err("archive_direct_unsupported");
    }
    let low_packed = u64::from(read_u32(header, 7)?);
    let low_unpacked = u64::from(read_u32(header, 11)?);
    let host_os = header[15];
    let method = header[25];
    let name_size = usize::from(read_u16(header, 26)?);
    let attributes = read_u32(header, 28)?;
    if matches!(host_os, 3 | 5) && attributes & 0xf000 == 0xa000 || method != 0x30 {
        return Err("archive_direct_unsupported");
    }
    let mut cursor = 32usize;
    let (packed_size, unpacked_size) = if flags & 0x0100 != 0 {
        let high_packed = u64::from(read_u32(header, cursor)?);
        let high_unpacked = u64::from(read_u32(header, cursor + 4)?);
        cursor += 8;
        (
            high_packed << 32 | low_packed,
            high_unpacked << 32 | low_unpacked,
        )
    } else {
        if low_unpacked == u64::from(u32::MAX) {
            return Err("archive_direct_unsupported");
        }
        (low_packed, low_unpacked)
    };
    if packed_size == 0
        || unpacked_size == 0
        || unpacked_size > MAX_LOGICAL_BYTES
        || packed_size > unpacked_size
    {
        return Err("archive_direct_unsupported");
    }
    let name_end = cursor
        .checked_add(name_size)
        .ok_or("archive_header_invalid")?;
    let raw_name = header
        .get(cursor..name_end)
        .ok_or("archive_header_incomplete")?;
    let raw_name = raw_name
        .split(|byte| *byte == 0)
        .next()
        .expect("split always yields one item");
    let raw_name = std::str::from_utf8(raw_name).map_err(|_| "archive_path_invalid")?;
    Ok(ParsedFile {
        relative_path: normalize_archive_path(raw_name)?,
        exact_size: unpacked_size,
        fragment_size: packed_size,
        split_before: flags & 0x0001 != 0,
        split_after: flags & 0x0002 != 0,
    })
}

fn read_u16(input: &[u8], offset: usize) -> Result<u16, &'static str> {
    Ok(u16::from_le_bytes(
        input
            .get(offset..offset + 2)
            .ok_or("archive_header_incomplete")?
            .try_into()
            .expect("bounded u16"),
    ))
}

fn read_u32(input: &[u8], offset: usize) -> Result<u32, &'static str> {
    Ok(u32::from_le_bytes(
        input
            .get(offset..offset + 4)
            .ok_or("archive_header_incomplete")?
            .try_into()
            .expect("bounded u32"),
    ))
}

#[cfg(test)]
mod tests {
    use super::{parse_rar4_stored_members, parse_rar5_stored_members};

    fn vint(mut value: u64) -> Vec<u8> {
        let mut encoded = Vec::new();
        loop {
            let mut byte = (value & 0x7f) as u8;
            value >>= 7;
            if value != 0 {
                byte |= 0x80;
            }
            encoded.push(byte);
            if value == 0 {
                return encoded;
            }
        }
    }

    fn block_with_common_flags(
        kind: u64,
        common_flags: u64,
        flags: u64,
        body: &[u8],
        data: &[u8],
    ) -> Vec<u8> {
        let mut header = vint(kind);
        header.extend(vint(
            common_flags | flags | if data.is_empty() { 0 } else { 0x0002 },
        ));
        if !data.is_empty() {
            header.extend(vint(data.len() as u64));
        }
        header.extend_from_slice(body);
        let size = vint(header.len() as u64);
        let mut output = vec![0; 4];
        output.extend_from_slice(&size);
        output.extend_from_slice(&header);
        let crc = crc32fast::hash(&output[4..]);
        output[..4].copy_from_slice(&crc.to_le_bytes());
        output.extend_from_slice(data);
        output
    }

    fn block(kind: u64, flags: u64, body: &[u8], data: &[u8]) -> Vec<u8> {
        block_with_common_flags(kind, 0, flags, body, data)
    }

    fn main(volume: bool, number: Option<u64>) -> Vec<u8> {
        let mut body = vint(u64::from(volume) | if number.is_some() { 2 } else { 0 });
        if let Some(number) = number {
            body.extend(vint(number));
        }
        block_with_common_flags(1, 0x0004, 0, &body, &[])
    }

    fn file(
        name: &str,
        data: &[u8],
        exact_size: u64,
        crc_contents: &[u8],
        before: bool,
        after: bool,
        mode: (u64, u64),
    ) -> Vec<u8> {
        let (compression, host_os) = mode;
        let mut body = vint(0x0004);
        body.extend(vint(exact_size));
        body.extend(vint(0));
        body.extend_from_slice(&crc32fast::hash(crc_contents).to_le_bytes());
        body.extend(vint(compression));
        body.extend(vint(host_os));
        body.extend(vint(name.len() as u64));
        body.extend_from_slice(name.as_bytes());
        block(
            2,
            (u64::from(before) * 0x0008) | (u64::from(after) * 0x0010),
            &body,
            data,
        )
    }

    fn end(next: bool) -> Vec<u8> {
        block_with_common_flags(5, 0x0004, 0, &vint(u64::from(next)), &[])
    }

    fn volume(number: Option<u64>, files: &[Vec<u8>], next: bool) -> Vec<u8> {
        let mut bytes = b"Rar!\x1a\x07\x01\0".to_vec();
        bytes.extend(main(next || number.is_some(), number));
        for file in files {
            bytes.extend_from_slice(file);
        }
        bytes.extend(end(next));
        bytes
    }

    fn parse(volumes: &[&[u8]]) -> Result<Vec<super::StoredMember>, &'static str> {
        let sizes = volumes
            .iter()
            .map(|volume| volume.len() as u64)
            .collect::<Vec<_>>();
        parse_rar5_stored_members(&sizes, |volume_index, offset, length| {
            let start = usize::try_from(offset).map_err(|_| "archive_header_invalid")?;
            let end = start.checked_add(length).ok_or("archive_header_invalid")?;
            volumes
                .get(volume_index)
                .and_then(|volume| volume.get(start..end))
                .map(<[u8]>::to_vec)
                .ok_or("archive_header_incomplete")
        })
    }

    fn rar4_block(kind: u8, flags: u16, body: &[u8], data: &[u8]) -> Vec<u8> {
        let mut output = vec![0; 2];
        output.push(kind);
        output.extend_from_slice(&flags.to_le_bytes());
        output.extend_from_slice(
            &u16::try_from(7 + body.len())
                .expect("small RAR4 header")
                .to_le_bytes(),
        );
        output.extend_from_slice(body);
        let crc = crc32fast::hash(&output[2..]) as u16;
        output[..2].copy_from_slice(&crc.to_le_bytes());
        output.extend_from_slice(data);
        output
    }

    fn rar4_file(
        name: &str,
        data: &[u8],
        exact_size: u32,
        crc_contents: &[u8],
        before: bool,
        after: bool,
        mode: (u8, u8),
    ) -> Vec<u8> {
        let (method, host_os) = mode;
        let mut body = Vec::new();
        body.extend_from_slice(&(data.len() as u32).to_le_bytes());
        body.extend_from_slice(&exact_size.to_le_bytes());
        body.push(host_os);
        body.extend_from_slice(&crc32fast::hash(crc_contents).to_le_bytes());
        body.extend_from_slice(&0_u32.to_le_bytes());
        body.push(15);
        body.push(method);
        body.extend_from_slice(&(name.len() as u16).to_le_bytes());
        body.extend_from_slice(&0_u32.to_le_bytes());
        body.extend_from_slice(name.as_bytes());
        rar4_block(
            0x74,
            0x8000 | u16::from(before) | (u16::from(after) * 2),
            &body,
            data,
        )
    }

    fn rar4_volume(first: bool, files: &[Vec<u8>], next: bool) -> Vec<u8> {
        let mut bytes = b"Rar!\x1a\x07\0".to_vec();
        let volume = next || !first;
        bytes.extend(rar4_block(
            0x73,
            if volume {
                0x0011 | if first { 0x0100 } else { 0 }
            } else {
                0
            },
            &[1, 0, 0, 0, 0, 0],
            &[],
        ));
        for file in files {
            bytes.extend_from_slice(file);
        }
        bytes.extend(rar4_block(0x7b, 0x4000 | u16::from(next), &[], &[]));
        bytes
    }

    fn parse4(volumes: &[&[u8]]) -> Result<Vec<super::StoredMember>, &'static str> {
        let sizes = volumes
            .iter()
            .map(|volume| volume.len() as u64)
            .collect::<Vec<_>>();
        parse_rar4_stored_members(&sizes, |volume_index, offset, length| {
            let start = usize::try_from(offset).map_err(|_| "archive_header_invalid")?;
            let end = start.checked_add(length).ok_or("archive_header_invalid")?;
            volumes
                .get(volume_index)
                .and_then(|volume| volume.get(start..end))
                .map(<[u8]>::to_vec)
                .ok_or("archive_header_incomplete")
        })
    }

    #[test]
    fn maps_only_complete_stored_non_solid_split_members() {
        let mut first = volume(
            Some(0),
            &[file("Movie.mkv", b"DATA", 8, b"DATA", false, true, (0, 1))],
            true,
        );
        first.extend_from_slice(&[0; 8]);
        let second = volume(
            Some(1),
            &[file(
                "Movie.mkv",
                b"MORE",
                8,
                b"DATAMORE",
                true,
                false,
                (0, 1),
            )],
            false,
        );

        let members = parse(&[&first, &second]).unwrap();

        assert_eq!(members.len(), 1);
        assert_eq!(members[0].relative_path, "Movie.mkv");
        assert_eq!(members[0].exact_size, 8);
        assert_eq!(members[0].ranges.len(), 2);
        assert_eq!(members[0].ranges[0].volume_index, 0);
        assert_eq!(&first[members[0].ranges[0].offset as usize..][..4], b"DATA");
        assert_eq!(
            &second[members[0].ranges[1].offset as usize..][..4],
            b"MORE"
        );
        let future_host = volume(
            None,
            &[file(
                "Folder\\Vidéo.mkv",
                b"DATA",
                4,
                b"DATA",
                false,
                false,
                (0, 7),
            )],
            false,
        );
        let mut large_zero_padding = first.clone();
        large_zero_padding.resize(large_zero_padding.len() + 128 * 1024, 0);
        assert!(parse(&[&large_zero_padding, &second]).is_ok());
        assert_eq!(
            parse(&[&future_host])
                .expect("future RAR5 host")
                .remove(0)
                .relative_path,
            "Folder/Vidéo.mkv"
        );
    }

    #[test]
    fn rejects_compression_solid_encryption_bad_header_crc_and_split_conflicts() {
        let compressed = volume(
            None,
            &[file(
                "Movie.mkv",
                b"DATA",
                4,
                b"DATA",
                false,
                false,
                (0x80, 1),
            )],
            false,
        );
        assert_eq!(parse(&[&compressed]), Err("archive_direct_unsupported"));

        let mut solid = b"Rar!\x1a\x07\x01\0".to_vec();
        solid.extend(block(1, 0, &vint(0x0004), &[]));
        solid.extend(end(false));
        assert_eq!(parse(&[&solid]), Err("archive_direct_unsupported"));

        let mut encrypted = b"Rar!\x1a\x07\x01\0".to_vec();
        encrypted.extend(main(false, None));
        encrypted.extend(block(4, 0, &vint(0), &[]));
        encrypted.extend(end(false));
        assert_eq!(parse(&[&encrypted]), Err("archive_direct_unsupported"));

        let mut corrupt = volume(
            None,
            &[file("Movie.mkv", b"DATA", 4, b"DATA", false, false, (0, 1))],
            false,
        );
        corrupt[12] ^= 1;
        assert_eq!(parse(&[&corrupt]), Err("archive_header_invalid"));

        let dangling = volume(
            None,
            &[file("Movie.mkv", b"DATA", 4, b"DATA", false, true, (0, 1))],
            false,
        );
        assert_eq!(parse(&[&dangling]), Err("archive_volume_conflict"));

        let short = volume(
            None,
            &[file("Movie.mkv", b"DATA", 5, b"DATA", false, false, (0, 1))],
            false,
        );
        assert_eq!(parse(&[&short]), Err("archive_volume_conflict"));

        let first = volume(
            None,
            &[file("Movie.mkv", b"DATA", 8, b"DATA", false, true, (0, 1))],
            true,
        );
        let mismatched = volume(
            Some(1),
            &[file(
                "Movie.mkv",
                b"MORE",
                9,
                b"DATAMORE",
                true,
                false,
                (0, 1),
            )],
            false,
        );
        assert_eq!(
            parse(&[&first, &mismatched]),
            Err("archive_volume_conflict")
        );
        let duplicate_path = volume(
            None,
            &[
                file("Movie.mkv", b"DATA", 4, b"DATA", false, false, (0, 1)),
                file("Movie.mkv", b"MORE", 4, b"MORE", false, false, (0, 1)),
            ],
            false,
        );
        assert_eq!(parse(&[&duplicate_path]), Err("archive_volume_conflict"));

        let mut padded_terminal = volume(
            None,
            &[file("Movie.mkv", b"DATA", 4, b"DATA", false, false, (0, 1))],
            false,
        );
        padded_terminal.push(0);
        assert_eq!(parse(&[&padded_terminal]), Err("archive_header_invalid"));

        let mut nonzero_padding = first.clone();
        nonzero_padding.push(1);
        let second = volume(
            Some(1),
            &[file(
                "Movie.mkv",
                b"MORE",
                8,
                b"DATAMORE",
                true,
                false,
                (0, 1),
            )],
            false,
        );
        assert!(parse(&[&nonzero_padding, &second]).is_ok());
    }

    #[test]
    fn maps_complete_rar4_stored_split_members_and_rejects_unsafe_modes() {
        let first = rar4_volume(
            true,
            &[rar4_file(
                "Movie.mkv",
                b"DATA",
                8,
                b"DATA",
                false,
                true,
                (0x30, 2),
            )],
            true,
        );
        let second = rar4_volume(
            false,
            &[rar4_file(
                "Movie.mkv",
                b"MORE",
                8,
                b"DATAMORE",
                true,
                false,
                (0x30, 2),
            )],
            false,
        );

        let members = parse4(&[&first, &second]).expect("RAR4 stored split");

        assert_eq!(members.len(), 1);
        assert_eq!(members[0].relative_path, "Movie.mkv");
        assert_eq!(members[0].exact_size, 8);
        assert_eq!(&first[members[0].ranges[0].offset as usize..][..4], b"DATA");
        assert_eq!(
            &second[members[0].ranges[1].offset as usize..][..4],
            b"MORE"
        );

        let metadata = {
            let raw_name = b"Movie.mkv\0unicode-name";
            let mut body = Vec::new();
            body.extend_from_slice(&4_u32.to_le_bytes());
            body.extend_from_slice(&4_u32.to_le_bytes());
            body.push(2);
            body.extend_from_slice(&crc32fast::hash(b"DATA").to_le_bytes());
            body.extend_from_slice(&0_u32.to_le_bytes());
            body.push(15);
            body.push(0x30);
            body.extend_from_slice(&(raw_name.len() as u16).to_le_bytes());
            body.extend_from_slice(&0_u32.to_le_bytes());
            body.extend_from_slice(raw_name);
            body.extend_from_slice(&[0x10, 0, 0, 0, 0]);
            rar4_volume(true, &[rar4_block(0x74, 0x9200, &body, b"DATA")], false)
        };
        assert_eq!(
            parse4(&[&metadata])
                .expect("RAR4 unicode name and extended time")
                .remove(0)
                .relative_path,
            "Movie.mkv"
        );

        let compressed = rar4_volume(
            true,
            &[rar4_file(
                "Movie.mkv",
                b"DATA",
                4,
                b"DATA",
                false,
                false,
                (0x31, 2),
            )],
            false,
        );
        assert_eq!(parse4(&[&compressed]), Err("archive_direct_unsupported"));
        let future_host = rar4_volume(
            true,
            &[rar4_file(
                "Vidéo.mkv",
                b"DATA",
                4,
                b"DATA",
                false,
                false,
                (0x30, 7),
            )],
            false,
        );
        assert_eq!(
            parse4(&[&future_host])
                .expect("future RAR4 host")
                .remove(0)
                .relative_path,
            "Vidéo.mkv"
        );
        let encrypted = {
            let mut bytes = b"Rar!\x1a\x07\0".to_vec();
            bytes.extend(rar4_block(0x73, 0x0080, &[0; 6], &[]));
            bytes.extend(rar4_block(0x7b, 0, &[], &[]));
            bytes
        };
        assert_eq!(parse4(&[&encrypted]), Err("archive_direct_unsupported"));
    }
}
