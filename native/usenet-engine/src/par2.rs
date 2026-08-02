use crate::archive::normalize_archive_path;
use crc32fast::Hasher as Crc32Hasher;
use md5::{Digest, Md5};
use sha2::Sha256;
use std::collections::{BTreeMap, BTreeSet};
use std::io::{Read, Seek, SeekFrom};

const MAGIC: &[u8; 8] = b"PAR2\0PKT";
const TYPE_MAIN: &[u8; 16] = b"PAR 2.0\0Main\0\0\0\0";
const TYPE_FILE_DESCRIPTION: &[u8; 16] = b"PAR 2.0\0FileDesc";
const TYPE_UNICODE_FILENAME: &[u8; 16] = b"PAR 2.0\0UniFileN";
const TYPE_IFSC: &[u8; 16] = b"PAR 2.0\0IFSC\0\0\0\0";
const TYPE_RECOVERY_SLICE: &[u8; 16] = b"PAR 2.0\0RecvSlic";

pub(crate) const MAX_IN_MEMORY_INPUT_BYTES: usize = 150 * 1024 * 1024;
const SCAN_BYTES: usize = 64 * 1024;
const MAX_PACKETS: usize = 100_000;
const MAX_FILES: usize = 20_000;
const MAX_SLICES: usize = 32_768;
const MAX_CATALOG_PACKET_BYTES: u64 = 64 + 16 + 20 * MAX_SLICES as u64;
const MAX_RECOVERY_SETS: usize = MAX_FILES;
const MAX_SOURCE_BYTES: u64 = crate::limits::MAX_LOGICAL_BYTES;

pub type FileId = [u8; 16];
pub type RecoverySetId = [u8; 16];
pub type RecoveryPacketRanges = Vec<Vec<(u64, u64)>>;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FileDescription {
    pub id: FileId,
    pub full_md5: [u8; 16],
    pub first_16k_md5: [u8; 16],
    pub byte_size: u64,
    pub relative_path: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SliceChecksum {
    pub md5: [u8; 16],
    pub crc32: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RecoverySet {
    pub set_id: RecoverySetId,
    pub slice_size: u64,
    pub source_file_ids: Vec<FileId>,
    pub files: BTreeMap<FileId, FileDescription>,
    pub checksums: BTreeMap<FileId, Vec<SliceChecksum>>,
    pub recovery_exponents: BTreeSet<u32>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DiscoveredRecoverySet {
    pub set: RecoverySet,
    pub input_indices: Vec<usize>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct MainPacket {
    slice_size: u64,
    source_file_ids: Vec<FileId>,
    all_file_ids: Vec<FileId>,
}

#[derive(Default)]
struct Catalog {
    set_id: Option<RecoverySetId>,
    main: Option<MainPacket>,
    files: BTreeMap<FileId, FileDescription>,
    unicode_paths: BTreeMap<FileId, String>,
    checksums: BTreeMap<FileId, Vec<SliceChecksum>>,
    recovery_slices: BTreeMap<u32, RecoverySliceEvidence>,
    slices: usize,
}

#[derive(Eq, PartialEq)]
struct RecoverySliceEvidence {
    length: u64,
    sha256: [u8; 32],
}

enum ReaderPacket<'a> {
    Catalog(&'a [u8]),
    RecoverySlice {
        exponent: u32,
        evidence: RecoverySliceEvidence,
    },
    Other,
}

#[derive(Default)]
struct RecoverySetDiscovery {
    catalogs: BTreeMap<RecoverySetId, (Catalog, BTreeSet<usize>)>,
    files: usize,
    slices: usize,
}

impl RecoverySetDiscovery {
    fn observe(
        &mut self,
        input_index: usize,
        set_id: RecoverySetId,
        packet: &[u8],
    ) -> Result<(), &'static str> {
        if !self.catalogs.contains_key(&set_id) && self.catalogs.len() >= MAX_RECOVERY_SETS {
            return Err("par2_recovery_set_limit");
        }
        let (catalog, input_indices) = self.catalogs.entry(set_id).or_default();
        let previous_files = catalog.files.len();
        let previous_slices = catalog.slices;
        parse_catalog_packet(catalog, packet)?;
        self.files += catalog.files.len() - previous_files;
        self.slices += catalog.slices - previous_slices;
        if self.files > MAX_FILES || self.slices > MAX_SLICES {
            return Err(if self.files > MAX_FILES {
                "par2_file_limit"
            } else {
                "par2_slice_limit"
            });
        }
        input_indices.insert(input_index);
        Ok(())
    }

    fn finish(self) -> Result<Vec<DiscoveredRecoverySet>, &'static str> {
        let mut discovered = Vec::with_capacity(self.catalogs.len());
        for (_set_id, (catalog, input_indices)) in self.catalogs {
            match catalog.finish() {
                Ok(set) => discovered.push(DiscoveredRecoverySet {
                    set,
                    input_indices: input_indices.into_iter().collect(),
                }),
                Err(
                    "par2_main_missing" | "par2_file_description_missing" | "par2_ifsc_missing",
                ) => {}
                Err(code) => return Err(code),
            }
        }
        if discovered.is_empty() {
            return Err("par2_recovery_set_missing");
        }
        Ok(discovered)
    }
}

fn array_16(bytes: &[u8]) -> [u8; 16] {
    bytes.try_into().expect("validated sixteen-byte PAR2 field")
}

fn md5(bytes: &[u8]) -> [u8; 16] {
    Md5::digest(bytes).into()
}

fn checked_packet_end(offset: usize, length: u64, input_len: usize) -> Option<usize> {
    let length = usize::try_from(length).ok()?;
    if !(64..=MAX_IN_MEMORY_INPUT_BYTES).contains(&length) || !length.is_multiple_of(4) {
        return None;
    }
    let end = offset.checked_add(length)?;
    if end > input_len {
        return None;
    }
    Some(end)
}

fn insert_exact<K: Ord, V: Eq>(
    entries: &mut BTreeMap<K, V>,
    key: K,
    value: V,
    conflict: &'static str,
) -> Result<(), &'static str> {
    if entries.get(&key).is_some_and(|existing| existing != &value) {
        return Err(conflict);
    }
    entries.entry(key).or_insert(value);
    Ok(())
}

fn parse_catalog_packet(catalog: &mut Catalog, packet: &[u8]) -> Result<(), &'static str> {
    match catalog.parse_packet(packet) {
        Ok(()) => Ok(()),
        Err(
            "par2_main_invalid"
            | "par2_file_description_invalid"
            | "par2_file_name_invalid"
            | "par2_file_id_mismatch"
            | "par2_unicode_file_name_invalid"
            | "par2_ifsc_invalid"
            | "par2_recovery_slice_invalid",
        ) => Ok(()),
        Err(code) => Err(code),
    }
}

fn parse_reader_packet(
    catalog: &mut Catalog,
    set_id: RecoverySetId,
    packet: ReaderPacket<'_>,
) -> Result<(), &'static str> {
    match packet {
        ReaderPacket::Catalog(packet) => parse_catalog_packet(catalog, packet),
        ReaderPacket::RecoverySlice { exponent, evidence } => {
            catalog.accept_set(set_id)?;
            if exponent >= u32::from(u16::MAX) {
                return Ok(());
            }
            insert_exact(
                &mut catalog.recovery_slices,
                exponent,
                evidence,
                "par2_recovery_slice_conflict",
            )
        }
        ReaderPacket::Other => Ok(()),
    }
}

impl Catalog {
    fn accept_set(&mut self, set_id: RecoverySetId) -> Result<(), &'static str> {
        if self.set_id.is_some_and(|existing| existing != set_id) {
            return Err("par2_recovery_set_mismatch");
        }
        self.set_id = Some(set_id);
        Ok(())
    }

    fn parse_packet(&mut self, packet: &[u8]) -> Result<(), &'static str> {
        let set_id = array_16(&packet[32..48]);
        self.accept_set(set_id)?;
        let packet_type = &packet[48..64];
        let body = &packet[64..];
        if packet_type == TYPE_MAIN {
            self.parse_main(body)
        } else if packet_type == TYPE_FILE_DESCRIPTION {
            self.parse_file_description(body)
        } else if packet_type == TYPE_UNICODE_FILENAME {
            self.parse_unicode_filename(body)
        } else if packet_type == TYPE_IFSC {
            self.parse_ifsc(body)
        } else if packet_type == TYPE_RECOVERY_SLICE {
            self.parse_recovery_slice(body)
        } else {
            Ok(())
        }
    }

    fn parse_main(&mut self, body: &[u8]) -> Result<(), &'static str> {
        if body.len() < 12 || !(body.len() - 12).is_multiple_of(16) {
            return Err("par2_main_invalid");
        }
        let slice_size = u64::from_le_bytes(body[..8].try_into().expect("PAR2 slice size"));
        let recoverable_files = usize::try_from(u32::from_le_bytes(
            body[8..12].try_into().expect("PAR2 recoverable count"),
        ))
        .map_err(|_| "par2_main_invalid")?;
        let total_files = (body.len() - 12) / 16;
        if slice_size == 0
            || slice_size > MAX_SOURCE_BYTES
            || !slice_size.is_multiple_of(4)
            || recoverable_files == 0
            || recoverable_files > total_files
            || total_files > MAX_FILES
        {
            return Err("par2_main_invalid");
        }
        let all_file_ids = body[12..]
            .chunks_exact(16)
            .map(array_16)
            .collect::<Vec<_>>();
        let source_file_ids = all_file_ids[..recoverable_files].to_vec();
        if all_file_ids.iter().copied().collect::<BTreeSet<_>>().len() != total_files {
            return Err("par2_main_invalid");
        }
        let main = MainPacket {
            slice_size,
            source_file_ids,
            all_file_ids,
        };
        if self.main.as_ref().is_some_and(|existing| existing != &main) {
            return Err("par2_main_conflict");
        }
        self.main = Some(main);
        Ok(())
    }

    fn parse_file_description(&mut self, body: &[u8]) -> Result<(), &'static str> {
        if body.len() < 60 {
            return Err("par2_file_description_invalid");
        }
        let id = array_16(&body[..16]);
        let full_md5 = array_16(&body[16..32]);
        let first_16k_md5 = array_16(&body[32..48]);
        let byte_size = u64::from_le_bytes(body[48..56].try_into().expect("PAR2 file length"));
        let name_field = &body[56..];
        let padding = name_field
            .iter()
            .rev()
            .take_while(|byte| **byte == 0)
            .count();
        if padding > 3 || padding == name_field.len() {
            return Err("par2_file_description_invalid");
        }
        let raw_name = std::str::from_utf8(&name_field[..name_field.len() - padding])
            .map_err(|_| "par2_file_name_invalid")?;
        let relative_path =
            normalize_archive_path(raw_name).map_err(|_| "par2_file_name_invalid")?;
        let mut identity = Md5::new();
        identity.update(first_16k_md5);
        identity.update(byte_size.to_le_bytes());
        identity.update(raw_name.as_bytes());
        if <[u8; 16]>::from(identity.finalize()) != id {
            return Err("par2_file_id_mismatch");
        }
        if self.files.len() >= MAX_FILES && !self.files.contains_key(&id) {
            return Err("par2_file_limit");
        }
        insert_exact(
            &mut self.files,
            id,
            FileDescription {
                id,
                full_md5,
                first_16k_md5,
                byte_size,
                relative_path,
            },
            "par2_file_description_conflict",
        )
    }

    fn parse_unicode_filename(&mut self, body: &[u8]) -> Result<(), &'static str> {
        if body.len() < 18 || !(body.len() - 16).is_multiple_of(2) {
            return Err("par2_unicode_file_name_invalid");
        }
        let id = array_16(&body[..16]);
        let mut units = body[16..]
            .chunks_exact(2)
            .map(|unit| u16::from_le_bytes(unit.try_into().expect("PAR2 Unicode code unit")))
            .collect::<Vec<_>>();
        if units.len().is_multiple_of(2) && units.last() == Some(&0) {
            units.pop();
        }
        if units.is_empty() || units.contains(&0) {
            return Err("par2_unicode_file_name_invalid");
        }
        let raw_name = char::decode_utf16(units)
            .collect::<Result<String, _>>()
            .map_err(|_| "par2_unicode_file_name_invalid")?;
        let relative_path =
            normalize_archive_path(&raw_name).map_err(|_| "par2_unicode_file_name_invalid")?;
        if self.unicode_paths.len() >= MAX_FILES && !self.unicode_paths.contains_key(&id) {
            return Err("par2_file_limit");
        }
        insert_exact(
            &mut self.unicode_paths,
            id,
            relative_path,
            "par2_unicode_file_name_conflict",
        )
    }

    fn parse_ifsc(&mut self, body: &[u8]) -> Result<(), &'static str> {
        if body.len() < 16 || !(body.len() - 16).is_multiple_of(20) {
            return Err("par2_ifsc_invalid");
        }
        let id = array_16(&body[..16]);
        let slice_count = (body.len() - 16) / 20;
        if let Some(existing) = self.checksums.get(&id) {
            return (existing.len() == slice_count
                && existing
                    .iter()
                    .zip(body[16..].chunks_exact(20))
                    .all(|(existing, entry)| {
                        *existing
                            == SliceChecksum {
                                md5: array_16(&entry[..16]),
                                crc32: u32::from_le_bytes(
                                    entry[16..20].try_into().expect("PAR2 slice CRC"),
                                ),
                            }
                    }))
            .then_some(())
            .ok_or("par2_ifsc_conflict");
        }
        if self.checksums.len() >= MAX_FILES {
            return Err("par2_file_limit");
        }
        if self.slices + slice_count > MAX_SLICES {
            return Err("par2_slice_limit");
        }
        let checksums = body[16..]
            .chunks_exact(20)
            .map(|entry| SliceChecksum {
                md5: array_16(&entry[..16]),
                crc32: u32::from_le_bytes(entry[16..20].try_into().expect("PAR2 slice CRC")),
            })
            .collect::<Vec<_>>();
        insert_exact(&mut self.checksums, id, checksums, "par2_ifsc_conflict")?;
        self.slices += slice_count;
        Ok(())
    }

    fn parse_recovery_slice(&mut self, body: &[u8]) -> Result<(), &'static str> {
        if body.len() < 4 {
            return Err("par2_recovery_slice_invalid");
        }
        let exponent = u32::from_le_bytes(body[..4].try_into().expect("PAR2 recovery exponent"));
        if exponent >= u32::from(u16::MAX) {
            return Err("par2_recovery_slice_invalid");
        }
        insert_exact(
            &mut self.recovery_slices,
            exponent,
            RecoverySliceEvidence {
                length: (body.len() - 4) as u64,
                sha256: Sha256::digest(&body[4..]).into(),
            },
            "par2_recovery_slice_conflict",
        )
    }

    fn finish(mut self) -> Result<RecoverySet, &'static str> {
        let set_id = self.set_id.ok_or("par2_main_missing")?;
        let main = self.main.ok_or("par2_main_missing")?;
        let listed = main.all_file_ids.iter().copied().collect::<BTreeSet<_>>();
        self.files.retain(|id, _file| listed.contains(id));
        self.unicode_paths.retain(|id, _path| listed.contains(id));
        self.checksums.retain(|id, _checksums| listed.contains(id));
        for (id, unicode_path) in self.unicode_paths {
            let file = self
                .files
                .get_mut(&id)
                .ok_or("par2_file_description_missing")?;
            file.relative_path = unicode_path;
        }
        let mut paths = BTreeSet::new();
        for file in self.files.values() {
            if !paths.insert(file.relative_path.as_str()) {
                return Err("par2_file_name_conflict");
            }
        }
        let mut total_slices = 0usize;
        for id in &main.source_file_ids {
            let file = self.files.get(id).ok_or("par2_file_description_missing")?;
            let expected_slices = if file.byte_size == 0 {
                0
            } else {
                usize::try_from(file.byte_size.div_ceil(main.slice_size))
                    .map_err(|_| "par2_slice_count_invalid")?
            };
            let checksums = self.checksums.get(id).ok_or("par2_ifsc_missing")?;
            if checksums.len() != expected_slices {
                return Err("par2_slice_count_invalid");
            }
            total_slices += expected_slices;
        }
        if total_slices > MAX_SLICES {
            return Err("par2_slice_limit");
        }
        self.recovery_slices
            .retain(|_exponent, slice| slice.length == main.slice_size);
        Ok(RecoverySet {
            set_id,
            slice_size: main.slice_size,
            source_file_ids: main.source_file_ids,
            files: self.files,
            checksums: self.checksums,
            recovery_exponents: self.recovery_slices.into_keys().collect(),
        })
    }
}

pub fn parse_recovery_set(input: &[u8]) -> Result<RecoverySet, &'static str> {
    parse_recovery_volumes(&[input])
}

fn visit_packets<F>(inputs: &[&[u8]], mut visit: F) -> Result<(), &'static str>
where
    F: FnMut(usize, usize, RecoverySetId, &[u8]) -> Result<(), &'static str>,
{
    if inputs.is_empty() || inputs.len() > MAX_FILES {
        return Err("par2_input_invalid");
    }
    let mut total = 0usize;
    let mut packets = 0usize;
    for (input_index, input) in inputs.iter().enumerate() {
        if input.is_empty() {
            return Err("par2_input_invalid");
        }
        total = total.checked_add(input.len()).ok_or("par2_input_invalid")?;
        if total > MAX_IN_MEMORY_INPUT_BYTES {
            return Err("par2_input_invalid");
        }
        let mut offset = 0usize;
        while offset < input.len() {
            let Some(candidate) = memchr::memmem::find(&input[offset..], MAGIC) else {
                break;
            };
            offset += candidate;
            let Some(header) = input.get(offset..offset + 64) else {
                break;
            };
            packets += 1;
            if packets > MAX_PACKETS {
                return Err("par2_packet_limit");
            }
            let length = u64::from_le_bytes(header[8..16].try_into().expect("PAR2 packet length"));
            let Some(end) = checked_packet_end(offset, length, input.len()) else {
                offset += 1;
                continue;
            };
            let packet = &input[offset..end];
            if md5(&packet[32..]) != array_16(&packet[16..32]) {
                offset += 1;
                continue;
            }
            visit(input_index, offset, array_16(&packet[32..48]), packet)?;
            offset = end;
        }
    }
    Ok(())
}

fn visit_reader_packets<R, F, C>(
    inputs: &mut [R],
    cancelled: &C,
    mut visit: F,
) -> Result<(), &'static str>
where
    R: Read + Seek,
    F: for<'a> FnMut(usize, u64, u64, RecoverySetId, ReaderPacket<'a>) -> Result<(), &'static str>,
    C: Fn() -> bool,
{
    if inputs.is_empty() || inputs.len() > MAX_FILES {
        return Err("par2_input_invalid");
    }
    let mut total = 0u64;
    let mut packets = 0usize;
    let mut scan = vec![0u8; SCAN_BYTES];
    let mut packet = Vec::new();
    for (input_index, input) in inputs.iter_mut().enumerate() {
        let input_len = input
            .seek(SeekFrom::End(0))
            .map_err(|_| "materialization_unavailable")?;
        total = total
            .checked_add(input_len)
            .filter(|value| *value <= MAX_SOURCE_BYTES)
            .ok_or("par2_input_invalid")?;
        if input_len == 0 {
            return Err("par2_input_invalid");
        }
        let mut offset = 0u64;
        while offset < input_len {
            if cancelled() {
                return Err("materialization_cancelled");
            }
            input
                .seek(SeekFrom::Start(offset))
                .map_err(|_| "materialization_unavailable")?;
            let wanted = (input_len - offset).min(SCAN_BYTES as u64) as usize;
            input
                .read_exact(&mut scan[..wanted])
                .map_err(|_| "materialization_unavailable")?;
            let Some(candidate) = memchr::memmem::find(&scan[..wanted], MAGIC) else {
                if wanted <= MAGIC.len() {
                    break;
                }
                offset += (wanted - (MAGIC.len() - 1)) as u64;
                continue;
            };
            let packet_offset = offset + candidate as u64;
            input
                .seek(SeekFrom::Start(packet_offset))
                .map_err(|_| "materialization_unavailable")?;
            let mut header = [0u8; 64];
            match input.read_exact(&mut header) {
                Ok(()) => {}
                Err(error) if error.kind() == std::io::ErrorKind::UnexpectedEof => break,
                Err(_) => return Err("materialization_unavailable"),
            }
            packets += 1;
            if packets > MAX_PACKETS {
                return Err("par2_packet_limit");
            }
            let length = u64::from_le_bytes(header[8..16].try_into().expect("PAR2 packet length"));
            let valid_length = (64..=MAX_SOURCE_BYTES).contains(&length)
                && length.is_multiple_of(4)
                && packet_offset
                    .checked_add(length)
                    .is_some_and(|end| end <= input_len);
            if !valid_length {
                offset = packet_offset + 1;
                continue;
            }
            let set_id = array_16(&header[32..48]);
            let packet_type = &header[48..64];
            let observed = if packet_type == TYPE_RECOVERY_SLICE {
                let body_length = length - 64;
                if body_length < 4 {
                    offset = packet_offset + 1;
                    continue;
                }
                let mut exponent = [0u8; 4];
                input
                    .read_exact(&mut exponent)
                    .map_err(|_| "materialization_unavailable")?;
                let mut packet_md5 = Md5::new();
                packet_md5.update(&header[32..]);
                packet_md5.update(exponent);
                let mut slice_sha256 = Sha256::new();
                let mut remaining = body_length - 4;
                while remaining != 0 {
                    if cancelled() {
                        return Err("materialization_cancelled");
                    }
                    let wanted = remaining.min(scan.len() as u64) as usize;
                    input
                        .read_exact(&mut scan[..wanted])
                        .map_err(|_| "materialization_unavailable")?;
                    packet_md5.update(&scan[..wanted]);
                    slice_sha256.update(&scan[..wanted]);
                    remaining -= wanted as u64;
                }
                if <[u8; 16]>::from(packet_md5.finalize()) != array_16(&header[16..32]) {
                    offset = packet_offset + 1;
                    continue;
                }
                ReaderPacket::RecoverySlice {
                    exponent: u32::from_le_bytes(exponent),
                    evidence: RecoverySliceEvidence {
                        length: body_length - 4,
                        sha256: slice_sha256.finalize().into(),
                    },
                }
            } else if packet_type == TYPE_MAIN
                || packet_type == TYPE_FILE_DESCRIPTION
                || packet_type == TYPE_UNICODE_FILENAME
                || packet_type == TYPE_IFSC
            {
                if length > MAX_CATALOG_PACKET_BYTES {
                    offset = packet_offset + 1;
                    continue;
                }
                packet.resize(length as usize, 0);
                packet[..64].copy_from_slice(&header);
                input
                    .read_exact(&mut packet[64..])
                    .map_err(|_| "materialization_unavailable")?;
                if md5(&packet[32..]) != array_16(&packet[16..32]) {
                    offset = packet_offset + 1;
                    continue;
                }
                ReaderPacket::Catalog(&packet)
            } else {
                let mut packet_md5 = Md5::new();
                packet_md5.update(&header[32..]);
                let mut remaining = length - 64;
                while remaining != 0 {
                    if cancelled() {
                        return Err("materialization_cancelled");
                    }
                    let wanted = remaining.min(scan.len() as u64) as usize;
                    input
                        .read_exact(&mut scan[..wanted])
                        .map_err(|_| "materialization_unavailable")?;
                    packet_md5.update(&scan[..wanted]);
                    remaining -= wanted as u64;
                }
                if <[u8; 16]>::from(packet_md5.finalize()) != array_16(&header[16..32]) {
                    offset = packet_offset + 1;
                    continue;
                }
                ReaderPacket::Other
            };
            visit(input_index, packet_offset, length, set_id, observed)?;
            offset = packet_offset + length;
        }
    }
    Ok(())
}

pub fn parse_recovery_volumes(inputs: &[&[u8]]) -> Result<RecoverySet, &'static str> {
    let mut catalog = Catalog::default();
    visit_packets(inputs, |_input_index, _offset, _set_id, packet| {
        parse_catalog_packet(&mut catalog, packet)
    })?;
    catalog.finish()
}

pub fn parse_recovery_readers_for_set<R, F>(
    inputs: &mut [R],
    selected_set_id: RecoverySetId,
    cancelled: &F,
) -> Result<(RecoverySet, RecoveryPacketRanges), &'static str>
where
    R: Read + Seek,
    F: Fn() -> bool,
{
    let mut catalog = Catalog::default();
    let mut selected_ranges = vec![Vec::new(); inputs.len()];
    visit_reader_packets(
        inputs,
        cancelled,
        |input_index, offset, length, set_id, packet| {
            if set_id == selected_set_id {
                parse_reader_packet(&mut catalog, set_id, packet)?;
                selected_ranges[input_index].push((offset, length));
            }
            Ok(())
        },
    )?;
    if selected_ranges.iter().all(Vec::is_empty) {
        return Err("par2_recovery_set_missing");
    }
    Ok((catalog.finish()?, selected_ranges))
}

pub fn parse_recovery_volumes_for_set_in_place(
    inputs: &mut Vec<Vec<u8>>,
    selected_set_id: RecoverySetId,
) -> Result<RecoverySet, &'static str> {
    let mut catalog = Catalog::default();
    let mut found = false;
    let mut selected_ranges = vec![Vec::<(usize, usize)>::new(); inputs.len()];
    {
        let references = inputs.iter().map(Vec::as_slice).collect::<Vec<_>>();
        visit_packets(&references, |input_index, offset, set_id, packet| {
            if set_id == selected_set_id {
                found = true;
                parse_catalog_packet(&mut catalog, packet)?;
                selected_ranges[input_index].push((offset, offset + packet.len()));
            }
            Ok(())
        })?;
    }
    if !found {
        return Err("par2_recovery_set_missing");
    }
    let set = catalog.finish()?;
    for (input, ranges) in inputs.iter_mut().zip(selected_ranges) {
        let mut write_offset = 0usize;
        for (start, end) in ranges {
            input.copy_within(start..end, write_offset);
            write_offset += end - start;
        }
        input.truncate(write_offset);
    }
    inputs.retain(|input| !input.is_empty());
    Ok(set)
}

pub fn discover_recovery_sets(
    inputs: &[&[u8]],
) -> Result<Vec<DiscoveredRecoverySet>, &'static str> {
    let mut discovery = RecoverySetDiscovery::default();
    visit_packets(inputs, |input_index, _offset, set_id, packet| {
        discovery.observe(input_index, set_id, packet)
    })?;
    discovery.finish()
}

pub fn discover_recovery_sets_from_readers<R, F>(
    inputs: &mut [R],
    cancelled: &F,
) -> Result<Vec<DiscoveredRecoverySet>, &'static str>
where
    R: Read + Seek,
    F: Fn() -> bool,
{
    let mut discovery = RecoverySetDiscovery::default();
    visit_reader_packets(
        inputs,
        cancelled,
        |input_index, _offset, _length, set_id, packet| {
            if matches!(&packet, ReaderPacket::Other) {
                return Ok(());
            }
            if !discovery.catalogs.contains_key(&set_id)
                && discovery.catalogs.len() >= MAX_RECOVERY_SETS
            {
                return Err("par2_recovery_set_limit");
            }
            let (catalog, input_indices) = discovery.catalogs.entry(set_id).or_default();
            let previous_files = catalog.files.len();
            let previous_slices = catalog.slices;
            parse_reader_packet(catalog, set_id, packet)?;
            discovery.files += catalog.files.len() - previous_files;
            discovery.slices += catalog.slices - previous_slices;
            if discovery.files > MAX_FILES || discovery.slices > MAX_SLICES {
                return Err(if discovery.files > MAX_FILES {
                    "par2_file_limit"
                } else {
                    "par2_slice_limit"
                });
            }
            input_indices.insert(input_index);
            Ok(())
        },
    )?;
    discovery.finish()
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SourceSliceNeed {
    pub file_id: FileId,
    pub slice_index: usize,
    pub selected_asset: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RepairPlan {
    pub unknown_slices: Vec<SourceSliceNeed>,
    pub recovery_exponents: Vec<u32>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PartialSourceMapping {
    pub file_id: FileId,
    pub valid_slices: Vec<bool>,
    pub complete: bool,
}

pub struct PartialSourceEvidence {
    pub exact_size: u64,
    pub checksums: Vec<Option<SliceChecksum>>,
    pub full_md5: Option<[u8; 16]>,
    pub first_16k_md5: Option<[u8; 16]>,
}

fn pad_slice_hashes(md5: &mut Md5, crc32: &mut Crc32Hasher, padding: usize) {
    const ZEROS: [u8; 4096] = [0; 4096];
    let mut remaining = padding;
    while remaining != 0 {
        let length = remaining.min(ZEROS.len());
        md5.update(&ZEROS[..length]);
        crc32.update(&ZEROS[..length]);
        remaining -= length;
    }
}

#[allow(dead_code)]
pub(crate) fn padded_slice_checksum(bytes: &[u8], slice_size: usize) -> SliceChecksum {
    let mut md5 = Md5::new();
    let mut crc32 = Crc32Hasher::new();
    md5.update(bytes);
    crc32.update(bytes);
    pad_slice_hashes(&mut md5, &mut crc32, slice_size - bytes.len());
    SliceChecksum {
        md5: md5.finalize().into(),
        crc32: crc32.finalize(),
    }
}

pub fn identify_partial_source_evidence(
    set: &RecoverySet,
    evidence: &PartialSourceEvidence,
) -> Result<PartialSourceMapping, &'static str> {
    let complete = evidence.full_md5.is_some();
    let mut matches = set.source_file_ids.iter().filter_map(|file_id| {
        let description = set
            .files
            .get(file_id)
            .expect("recovery source has a file description");
        let expected = set
            .checksums
            .get(file_id)
            .expect("recovery source has slice checksums");
        if description.byte_size != evidence.exact_size
            || expected.len() != evidence.checksums.len()
            || evidence
                .checksums
                .iter()
                .zip(expected)
                .any(|(actual, expected)| actual.is_some_and(|actual| actual != *expected))
            || evidence
                .full_md5
                .is_some_and(|actual| actual != description.full_md5)
            || evidence
                .first_16k_md5
                .is_some_and(|actual| actual != description.first_16k_md5)
        {
            return None;
        }
        Some(*file_id)
    });
    let file_id = matches.next().ok_or("par2_source_unmatched")?;
    if matches.next().is_some() {
        return Err("par2_source_ambiguous");
    }
    Ok(PartialSourceMapping {
        file_id,
        valid_slices: evidence.checksums.iter().map(Option::is_some).collect(),
        complete,
    })
}

struct SourceEvidence {
    full_md5: [u8; 16],
    first_16k_md5: [u8; 16],
    checksums: Vec<SliceChecksum>,
}

fn read_source_evidence<R, F>(
    input: &mut R,
    exact_size: u64,
    slice_size: u64,
    cancelled: &F,
) -> Result<SourceEvidence, &'static str>
where
    R: Read,
    F: Fn() -> bool,
{
    let slice_size = slice_size as usize;
    let mut full_md5 = Md5::new();
    let mut first_16k_md5 = Md5::new();
    let mut slice_md5 = Md5::new();
    let mut slice_crc32 = Crc32Hasher::new();
    let mut slice_bytes = 0usize;
    let mut processed = 0u64;
    let mut checksums = Vec::new();
    let mut buffer = [0u8; 64 * 1024];
    while processed < exact_size {
        if cancelled() {
            return Err("materialization_cancelled");
        }
        let wanted = (exact_size - processed).min(buffer.len() as u64) as usize;
        let length = input
            .read(&mut buffer[..wanted])
            .map_err(|_| "materialization_unavailable")?;
        if length == 0 {
            return Err("par2_source_evidence_invalid");
        }
        let bytes = &buffer[..length];
        full_md5.update(bytes);
        if processed < 16 * 1024 {
            let first_length = (16 * 1024_u64 - processed).min(length as u64) as usize;
            first_16k_md5.update(&bytes[..first_length]);
        }
        let mut offset = 0usize;
        while offset < bytes.len() {
            let length = (slice_size - slice_bytes).min(bytes.len() - offset);
            let slice = &bytes[offset..offset + length];
            slice_md5.update(slice);
            slice_crc32.update(slice);
            slice_bytes += length;
            offset += length;
            if slice_bytes == slice_size {
                checksums.push(SliceChecksum {
                    md5: slice_md5.finalize_reset().into(),
                    crc32: std::mem::replace(&mut slice_crc32, Crc32Hasher::new()).finalize(),
                });
                slice_bytes = 0;
            }
        }
        processed += length as u64;
    }
    if slice_bytes != 0 {
        pad_slice_hashes(&mut slice_md5, &mut slice_crc32, slice_size - slice_bytes);
        checksums.push(SliceChecksum {
            md5: slice_md5.finalize().into(),
            crc32: slice_crc32.finalize(),
        });
    }
    if cancelled() {
        return Err("materialization_cancelled");
    }
    Ok(SourceEvidence {
        full_md5: full_md5.finalize().into(),
        first_16k_md5: first_16k_md5.finalize().into(),
        checksums,
    })
}

pub fn identify_complete_source<R, F>(
    set: &RecoverySet,
    input: &mut R,
    exact_size: u64,
    cancelled: &F,
) -> Result<FileId, &'static str>
where
    R: Read,
    F: Fn() -> bool,
{
    if !set.source_file_ids.iter().any(|file_id| {
        set.files
            .get(file_id)
            .expect("recovery source has a file description")
            .byte_size
            == exact_size
    }) {
        return Err("par2_source_unmatched");
    }
    let evidence = read_source_evidence(input, exact_size, set.slice_size, cancelled)?;
    let mut matches = set.source_file_ids.iter().copied().filter(|file_id| {
        let file = set
            .files
            .get(file_id)
            .expect("recovery source has a file description");
        file.byte_size == exact_size
            && file.full_md5 == evidence.full_md5
            && file.first_16k_md5 == evidence.first_16k_md5
            && set
                .checksums
                .get(file_id)
                .expect("recovery source has slice checksums")
                == &evidence.checksums
    });
    let file_id = matches.next().ok_or("par2_source_unmatched")?;
    if matches.next().is_some() {
        Err("par2_source_ambiguous")
    } else {
        Ok(file_id)
    }
}

pub fn verify_complete_source<R, F>(
    set: &RecoverySet,
    file_id: FileId,
    input: &mut R,
    exact_size: u64,
    cancelled: &F,
) -> Result<(), &'static str>
where
    R: Read,
    F: Fn() -> bool,
{
    let file = set
        .files
        .get(&file_id)
        .expect("selected repair source has a file description");
    let evidence = read_source_evidence(input, exact_size, set.slice_size, cancelled)?;
    if file.full_md5 != evidence.full_md5
        || file.first_16k_md5 != evidence.first_16k_md5
        || set
            .checksums
            .get(&file_id)
            .expect("selected repair source has slice checksums")
            != &evidence.checksums
    {
        return Err("par2_file_evidence_invalid");
    }
    Ok(())
}

pub fn plan_repair(
    set: &RecoverySet,
    selected_asset: FileId,
    known_valid: &BTreeMap<FileId, Vec<bool>>,
) -> Result<RepairPlan, &'static str> {
    let mut unknown_slices = Vec::new();
    for selected in [true, false] {
        for file_id in &set.source_file_ids {
            if (*file_id == selected_asset) != selected {
                continue;
            }
            let checksums = set
                .checksums
                .get(file_id)
                .expect("recovery source has slice checksums");
            let evidence = known_valid.get(file_id);
            for slice_index in 0..checksums.len() {
                if !evidence.is_some_and(|values| values[slice_index]) {
                    unknown_slices.push(SourceSliceNeed {
                        file_id: *file_id,
                        slice_index,
                        selected_asset: selected,
                    });
                }
            }
        }
    }
    if set.recovery_exponents.len() < unknown_slices.len() {
        return Err("repair_insufficient");
    }
    Ok(RepairPlan {
        recovery_exponents: set
            .recovery_exponents
            .iter()
            .take(unknown_slices.len())
            .copied()
            .collect(),
        unknown_slices,
    })
}

#[cfg(test)]
mod tests {
    use super::{
        FileId, MAX_IN_MEMORY_INPUT_BYTES, PartialSourceEvidence, RecoverySet, SCAN_BYTES,
        TYPE_FILE_DESCRIPTION, TYPE_IFSC, TYPE_MAIN, TYPE_RECOVERY_SLICE, TYPE_UNICODE_FILENAME,
        discover_recovery_sets, discover_recovery_sets_from_readers, identify_complete_source,
        identify_partial_source_evidence, parse_recovery_readers_for_set, parse_recovery_set,
        parse_recovery_volumes, parse_recovery_volumes_for_set_in_place, plan_repair,
        verify_complete_source,
    };
    use md5::{Digest, Md5};
    use std::collections::BTreeMap;
    use std::fs::{OpenOptions, remove_file};
    use std::io::{Cursor, Seek, SeekFrom, Write};

    fn md5(bytes: &[u8]) -> [u8; 16] {
        Md5::digest(bytes).into()
    }

    fn main_packet(body: &[u8]) -> Vec<u8> {
        packet_for_set(md5(body), TYPE_MAIN, body)
    }

    fn corpus_set_id(corpus: &[u8]) -> [u8; 16] {
        corpus[32..48].try_into().unwrap()
    }

    fn append_packet(corpus: &mut Vec<u8>, kind: &[u8; 16], body: &[u8]) {
        let packet = packet_for_set(corpus_set_id(corpus), kind, body);
        corpus.extend_from_slice(&packet);
    }

    fn packet_for_set(set_id: [u8; 16], kind: &[u8; 16], body: &[u8]) -> Vec<u8> {
        assert!((64 + body.len()).is_multiple_of(4));
        let mut packet = Vec::with_capacity(64 + body.len());
        packet.extend_from_slice(b"PAR2\0PKT");
        packet.extend_from_slice(&u64::try_from(64 + body.len()).unwrap().to_le_bytes());
        packet.extend_from_slice(&[0; 16]);
        packet.extend_from_slice(&set_id);
        packet.extend_from_slice(kind);
        packet.extend_from_slice(body);
        let digest = md5(&packet[32..]);
        packet[16..32].copy_from_slice(&digest);
        packet
    }

    fn replace_set_id(corpus: &mut [u8], set_id: [u8; 16]) {
        let mut offset = 0;
        while offset < corpus.len() {
            let length = usize::try_from(u64::from_le_bytes(
                corpus[offset + 8..offset + 16].try_into().unwrap(),
            ))
            .unwrap();
            let packet = &mut corpus[offset..offset + length];
            packet[32..48].copy_from_slice(&set_id);
            let digest = md5(&packet[32..]);
            packet[16..32].copy_from_slice(&digest);
            offset += length;
        }
    }

    fn file_id(first_16k: [u8; 16], size: u64, name: &str) -> FileId {
        let mut digest = Md5::new();
        digest.update(first_16k);
        digest.update(size.to_le_bytes());
        digest.update(name.as_bytes());
        digest.finalize().into()
    }

    fn file_description(name: &str, size: u64, marker: u8) -> (FileId, Vec<u8>) {
        let first = [marker; 16];
        let id = file_id(first, size, name);
        let mut body = Vec::new();
        body.extend_from_slice(&id);
        body.extend_from_slice(&[marker + 1; 16]);
        body.extend_from_slice(&first);
        body.extend_from_slice(&size.to_le_bytes());
        body.extend_from_slice(name.as_bytes());
        while !(64 + body.len()).is_multiple_of(4) {
            body.push(0);
        }
        (id, body)
    }

    fn unicode_filename(id: FileId, name: &str) -> Vec<u8> {
        let mut body = id.to_vec();
        for unit in name.encode_utf16() {
            body.extend_from_slice(&unit.to_le_bytes());
        }
        while !(64 + body.len()).is_multiple_of(4) {
            body.push(0);
        }
        body
    }

    fn ifsc(id: FileId, slices: usize, marker: u8) -> Vec<u8> {
        let mut body = id.to_vec();
        for index in 0..slices {
            body.extend_from_slice(&[marker + index as u8; 16]);
            body.extend_from_slice(&(index as u32).to_le_bytes());
        }
        body
    }

    fn verified_description(name: &str, bytes: &[u8]) -> (FileId, Vec<u8>) {
        let first = md5(&bytes[..bytes.len().min(16 * 1024)]);
        let id = file_id(first, bytes.len() as u64, name);
        let mut body = Vec::new();
        body.extend_from_slice(&id);
        body.extend_from_slice(&md5(bytes));
        body.extend_from_slice(&first);
        body.extend_from_slice(&(bytes.len() as u64).to_le_bytes());
        body.extend_from_slice(name.as_bytes());
        while !(64 + body.len()).is_multiple_of(4) {
            body.push(0);
        }
        (id, body)
    }

    fn verified_ifsc(id: FileId, bytes: &[u8], slice_size: usize) -> Vec<u8> {
        let mut body = id.to_vec();
        for slice in bytes.chunks(slice_size) {
            let checksum = super::padded_slice_checksum(slice, slice_size);
            body.extend_from_slice(&checksum.md5);
            body.extend_from_slice(&checksum.crc32.to_le_bytes());
        }
        body
    }

    fn partial_fixture(files: &[(&str, &[u8])]) -> (RecoverySet, Vec<FileId>) {
        let descriptions = files
            .iter()
            .map(|(name, bytes)| verified_description(name, bytes))
            .collect::<Vec<_>>();
        let mut ids = descriptions.iter().map(|(id, _)| *id).collect::<Vec<_>>();
        ids.sort();
        let mut main = 4_u64.to_le_bytes().to_vec();
        main.extend_from_slice(&(ids.len() as u32).to_le_bytes());
        for id in &ids {
            main.extend_from_slice(id);
        }
        let mut corpus = main_packet(&main);
        for (_id, description) in &descriptions {
            append_packet(&mut corpus, TYPE_FILE_DESCRIPTION, description);
        }
        for ((_, bytes), (id, _)) in files.iter().zip(&descriptions) {
            append_packet(&mut corpus, TYPE_IFSC, &verified_ifsc(*id, bytes, 4));
        }
        (
            parse_recovery_set(&corpus).unwrap(),
            descriptions.into_iter().map(|(id, _)| id).collect(),
        )
    }

    fn fixture(recovery_count: usize) -> (Vec<u8>, FileId, FileId) {
        fixture_with_markers(recovery_count, 1, 3)
    }

    fn fixture_with_markers(
        recovery_count: usize,
        selected_marker: u8,
        unrelated_marker: u8,
    ) -> (Vec<u8>, FileId, FileId) {
        let (selected, selected_description) =
            file_description("Show.S01E02.mkv", 8, selected_marker);
        let (unrelated, unrelated_description) =
            file_description("Show.S01E03.mkv", 8, unrelated_marker);
        let mut main = 4_u64.to_le_bytes().to_vec();
        main.extend_from_slice(&2_u32.to_le_bytes());
        let mut ordered_ids = [selected, unrelated];
        ordered_ids.sort();
        for id in ordered_ids {
            main.extend_from_slice(&id);
        }
        let mut corpus = main_packet(&main);
        append_packet(&mut corpus, TYPE_FILE_DESCRIPTION, &selected_description);
        append_packet(&mut corpus, TYPE_FILE_DESCRIPTION, &unrelated_description);
        append_packet(&mut corpus, TYPE_IFSC, &ifsc(selected, 2, 10));
        append_packet(&mut corpus, TYPE_IFSC, &ifsc(unrelated, 2, 20));
        for exponent in 0..recovery_count {
            let mut body = (exponent as u32).to_le_bytes().to_vec();
            body.extend_from_slice(&[exponent as u8; 4]);
            append_packet(&mut corpus, TYPE_RECOVERY_SLICE, &body);
        }
        (corpus, selected, unrelated)
    }

    #[test]
    fn parses_and_reconciles_a_complete_recovery_set() {
        let (corpus, selected, unrelated) = fixture(2);

        let set = parse_recovery_set(&corpus).unwrap();

        assert_eq!(set.slice_size, 4);
        let mut expected_ids = vec![selected, unrelated];
        expected_ids.sort();
        assert_eq!(set.source_file_ids, expected_ids);
        assert_eq!(set.files[&selected].relative_path, "Show.S01E02.mkv");
        assert_eq!(set.checksums[&unrelated].len(), 2);
        assert_eq!(set.recovery_exponents.len(), 2);
    }

    #[test]
    fn streams_recovery_packets_larger_than_one_nntp_article() {
        let slice_size = 16 * 1024 * 1024 + 4;
        let (file_id, description) = file_description("large-source.bin", slice_size as u64, 1);
        let mut main = (slice_size as u64).to_le_bytes().to_vec();
        main.extend_from_slice(&1_u32.to_le_bytes());
        main.extend_from_slice(&file_id);
        let mut corpus = main_packet(&main);
        append_packet(&mut corpus, TYPE_FILE_DESCRIPTION, &description);
        append_packet(&mut corpus, TYPE_IFSC, &ifsc(file_id, 1, 10));
        let mut recovery = 7_u32.to_le_bytes().to_vec();
        recovery.resize(4 + slice_size, 42);
        append_packet(&mut corpus, TYPE_RECOVERY_SLICE, &recovery);
        let set_id = corpus_set_id(&corpus);
        let mut readers = [Cursor::new(corpus)];

        let (set, ranges) =
            parse_recovery_readers_for_set(&mut readers, set_id, &|| false).unwrap();

        assert_eq!(set.slice_size, slice_size as u64);
        assert_eq!(set.recovery_exponents, [7].into());
        assert_eq!(ranges[0].len(), 4);
    }

    #[test]
    fn preserves_unsorted_main_file_order() {
        let (first, first_description) = file_description("first.bin", 8, 1);
        let (second, second_description) = file_description("second.bin", 8, 2);
        let mut ids = [first, second];
        ids.sort();
        ids.reverse();
        let mut main = 4_u64.to_le_bytes().to_vec();
        main.extend_from_slice(&2_u32.to_le_bytes());
        for id in ids {
            main.extend_from_slice(&id);
        }
        let mut corpus = main_packet(&main);
        append_packet(&mut corpus, TYPE_FILE_DESCRIPTION, &first_description);
        append_packet(&mut corpus, TYPE_FILE_DESCRIPTION, &second_description);
        append_packet(&mut corpus, TYPE_IFSC, &ifsc(first, 2, 10));
        append_packet(&mut corpus, TYPE_IFSC, &ifsc(second, 2, 20));

        let set = parse_recovery_set(&corpus).unwrap();

        assert_eq!(set.source_file_ids, ids);
    }

    #[test]
    fn ignores_an_invalid_main_packet_with_a_cross_half_duplicate_file_id() {
        let (selected, _) = file_description("Show.S01E02.mkv", 8, 1);
        let mut main = 4_u64.to_le_bytes().to_vec();
        main.extend_from_slice(&1_u32.to_le_bytes());
        main.extend_from_slice(&selected);
        main.extend_from_slice(&selected);
        let corpus = main_packet(&main);
        assert_eq!(parse_recovery_set(&corpus), Err("par2_main_missing"));
    }

    #[test]
    fn rejects_main_duplicates_that_change_only_non_recovery_membership() {
        let (mut corpus, selected, unrelated) = fixture(1);
        let (non_recovery, _description) = file_description("notes.txt", 4, 9);
        let mut conflicting_main = 4_u64.to_le_bytes().to_vec();
        conflicting_main.extend_from_slice(&2_u32.to_le_bytes());
        let mut recoverable = [selected, unrelated];
        recoverable.sort();
        for id in recoverable {
            conflicting_main.extend_from_slice(&id);
        }
        conflicting_main.extend_from_slice(&non_recovery);
        append_packet(&mut corpus, TYPE_MAIN, &conflicting_main);

        assert_eq!(parse_recovery_set(&corpus), Err("par2_main_conflict"));
    }

    #[test]
    fn ignores_file_metadata_not_listed_by_the_main_packet() {
        let (mut corpus, _, _) = fixture(1);
        let (extra_id, extra_description) = file_description("Show.S01E02.mkv", 8, 9);
        append_packet(&mut corpus, TYPE_FILE_DESCRIPTION, &extra_description);
        append_packet(&mut corpus, TYPE_IFSC, &ifsc(extra_id, 2, 30));
        append_packet(
            &mut corpus,
            TYPE_UNICODE_FILENAME,
            &unicode_filename([0x55; 16], "unlisted.mkv"),
        );

        let set = parse_recovery_set(&corpus).unwrap();

        assert_eq!(set.files.len(), 2);
        assert_eq!(set.checksums.len(), 2);
        assert!(!set.files.contains_key(&extra_id));
    }

    #[test]
    fn accepts_a_consistent_opaque_recovery_set_id() {
        let (mut corpus, _, _) = fixture(1);
        let opaque_set_id = [0x55; 16];
        replace_set_id(&mut corpus, opaque_set_id);

        assert_eq!(parse_recovery_set(&corpus).unwrap().set_id, opaque_set_id);
    }

    #[test]
    fn reconciles_complete_packets_across_independent_recovery_volumes() {
        let (corpus, _, _) = fixture(2);
        let first_packet_length =
            usize::try_from(u64::from_le_bytes(corpus[8..16].try_into().unwrap())).unwrap();

        let combined = parse_recovery_set(&corpus).unwrap();
        let split = parse_recovery_volumes(&[
            &corpus[..first_packet_length],
            &corpus[first_packet_length..],
        ])
        .unwrap();

        assert_eq!(split, combined);
    }

    #[test]
    fn discovers_complete_sets_across_sidecars_and_ignores_incomplete_sets() {
        let (first, _, _) = fixture(2);
        let second = fixture_with_markers(1, 5, 7).0;
        let first_set_id = corpus_set_id(&first);
        let second_set_id = corpus_set_id(&second);
        assert_ne!(first_set_id, second_set_id);
        let mut incomplete = 0_u32.to_le_bytes().to_vec();
        incomplete.extend_from_slice(&[0; 4]);
        let incomplete = packet_for_set([9; 16], TYPE_RECOVERY_SLICE, &incomplete);
        let first_packet_length =
            usize::try_from(u64::from_le_bytes(first[8..16].try_into().unwrap())).unwrap();
        let mut first_sidecar = first[..first_packet_length].to_vec();
        first_sidecar.extend_from_slice(&incomplete);
        let inputs = [
            first_sidecar.as_slice(),
            &first[first_packet_length..],
            second.as_slice(),
        ];

        let discovered = discover_recovery_sets(&inputs).unwrap();

        assert_eq!(discovered.len(), 2);
        let first_discovered = discovered
            .iter()
            .find(|entry| entry.set.set_id == first_set_id)
            .unwrap();
        let second_discovered = discovered
            .iter()
            .find(|entry| entry.set.set_id == second_set_id)
            .unwrap();
        assert_eq!(first_discovered.input_indices, [0, 1]);
        assert_eq!(second_discovered.input_indices, [2]);
        assert_eq!(
            parse_recovery_volumes(&inputs),
            Err("par2_recovery_set_mismatch")
        );
        let mut selected_inputs = inputs
            .iter()
            .map(|input| input.to_vec())
            .collect::<Vec<_>>();
        assert_eq!(
            parse_recovery_volumes_for_set_in_place(&mut selected_inputs, second_set_id).unwrap(),
            second_discovered.set
        );
    }

    #[test]
    fn streaming_discovery_and_selection_match_the_in_memory_parser() {
        let (corpus, _, _) = fixture(3);
        let set_id = corpus_set_id(&corpus);
        let prefix = vec![0u8; SCAN_BYTES - 4];
        let mut input = prefix;
        input.extend_from_slice(&corpus);
        let mut readers = [Cursor::new(input)];

        let discovered = discover_recovery_sets_from_readers(&mut readers, &|| false).unwrap();
        let (selected, ranges) =
            parse_recovery_readers_for_set(&mut readers, set_id, &|| false).unwrap();

        assert_eq!(discovered.len(), 1);
        assert_eq!(discovered[0].set, parse_recovery_set(&corpus).unwrap());
        assert_eq!(selected, discovered[0].set);
        assert_eq!(
            ranges[0]
                .iter()
                .map(|(_offset, length)| length)
                .sum::<u64>(),
            corpus.len() as u64
        );
    }

    #[test]
    fn streaming_discovery_accepts_sidecars_larger_than_the_memory_parser_budget() {
        let (corpus, _, _) = fixture(1);
        let set_id = corpus_set_id(&corpus);
        let path = std::env::temp_dir().join(format!(
            "comet-par2-streaming-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let mut input = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(true)
            .open(&path)
            .unwrap();
        input
            .set_len(MAX_IN_MEMORY_INPUT_BYTES as u64 + corpus.len() as u64 + 1)
            .unwrap();
        input
            .seek(SeekFrom::Start(MAX_IN_MEMORY_INPUT_BYTES as u64 + 1))
            .unwrap();
        input.write_all(&corpus).unwrap();

        let (selected, _ranges) =
            parse_recovery_readers_for_set(&mut [input], set_id, &|| false).unwrap();

        remove_file(path).unwrap();
        assert_eq!(selected, parse_recovery_set(&corpus).unwrap());
    }

    #[test]
    fn selected_set_compaction_removes_every_foreign_packet_in_place() {
        let foreign = fixture(2).0;
        let selected = fixture_with_markers(1, 5, 7).0;
        let selected_set_id = corpus_set_id(&selected);
        let mut mixed = foreign.clone();
        mixed.extend_from_slice(&selected);
        let original_capacity = mixed.capacity();
        let mut inputs = vec![foreign, mixed];

        let set = parse_recovery_volumes_for_set_in_place(&mut inputs, selected_set_id).unwrap();

        assert_eq!(set.set_id, selected_set_id);
        assert_eq!(inputs, [selected]);
        assert_eq!(inputs[0].capacity(), original_capacity);

        let original = inputs.clone();
        assert_eq!(
            parse_recovery_volumes_for_set_in_place(&mut inputs, [0x44; 16]),
            Err("par2_recovery_set_missing")
        );
        assert_eq!(inputs, original);
    }

    #[test]
    fn discovery_ignores_invalid_packets_and_requires_one_complete_set() {
        let mut incomplete = 0_u32.to_le_bytes().to_vec();
        incomplete.extend_from_slice(&[0; 4]);
        let incomplete = packet_for_set([9; 16], TYPE_RECOVERY_SLICE, &incomplete);
        assert_eq!(
            discover_recovery_sets(&[&incomplete]),
            Err("par2_recovery_set_missing")
        );

        let malformed = packet_for_set([8; 16], TYPE_IFSC, &[0; 20]);
        assert_eq!(
            discover_recovery_sets(&[&fixture(1).0, &malformed])
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn rejects_empty_inputs_and_ignores_packets_crossing_volume_boundaries() {
        let (corpus, _, _) = fixture(2);
        let first_packet_length =
            usize::try_from(u64::from_le_bytes(corpus[8..16].try_into().unwrap())).unwrap();

        assert_eq!(parse_recovery_volumes(&[]), Err("par2_input_invalid"));
        assert_eq!(
            parse_recovery_volumes(&[
                &corpus[..first_packet_length - 1],
                &corpus[first_packet_length - 1..]
            ]),
            Err("par2_main_missing")
        );
        assert_eq!(
            parse_recovery_volumes(&[&corpus, &[]]),
            Err("par2_input_invalid")
        );
    }

    #[test]
    fn applies_only_id_bound_safe_unicode_filename_overrides() {
        let (mut corpus, selected, _) = fixture(2);
        let unicode = unicode_filename(selected, "Série/Épisode 02.mkv");
        append_packet(&mut corpus, TYPE_UNICODE_FILENAME, &unicode);
        append_packet(&mut corpus, TYPE_UNICODE_FILENAME, &unicode);

        let set = parse_recovery_set(&corpus).unwrap();

        assert_eq!(set.files[&selected].relative_path, "Série/Épisode 02.mkv");

        let mut conflicting = corpus;
        append_packet(
            &mut conflicting,
            TYPE_UNICODE_FILENAME,
            &unicode_filename(selected, "Série/Autre.mkv"),
        );
        assert_eq!(
            parse_recovery_set(&conflicting),
            Err("par2_unicode_file_name_conflict")
        );
    }

    #[test]
    fn ignores_unbound_malformed_and_unsafe_unicode_filenames() {
        let (corpus, selected, _) = fixture(2);

        let mut unbound = corpus.clone();
        append_packet(
            &mut unbound,
            TYPE_UNICODE_FILENAME,
            &unicode_filename([0x55; 16], "unbound.mkv"),
        );
        assert!(parse_recovery_set(&unbound).is_ok());

        let mut unsafe_name = corpus.clone();
        append_packet(
            &mut unsafe_name,
            TYPE_UNICODE_FILENAME,
            &unicode_filename(selected, "../Évasion.mkv"),
        );
        assert_eq!(
            parse_recovery_set(&unsafe_name).unwrap().files[&selected].relative_path,
            "Show.S01E02.mkv"
        );

        let mut malformed_body = selected.to_vec();
        malformed_body.extend_from_slice(&0xd800_u16.to_le_bytes());
        malformed_body.extend_from_slice(&0_u16.to_le_bytes());
        let mut malformed = corpus;
        append_packet(&mut malformed, TYPE_UNICODE_FILENAME, &malformed_body);
        assert_eq!(
            parse_recovery_set(&malformed).unwrap().files[&selected].relative_path,
            "Show.S01E02.mkv"
        );
    }

    #[test]
    fn accepts_utf8_names_and_case_distinct_paths_but_rejects_exact_collisions() {
        let (utf8_id, description) = file_description("Série/Épisode.mkv", 4, 9);
        let mut main = 4_u64.to_le_bytes().to_vec();
        main.extend_from_slice(&1_u32.to_le_bytes());
        main.extend_from_slice(&utf8_id);
        let mut utf8 = main_packet(&main);
        append_packet(&mut utf8, TYPE_FILE_DESCRIPTION, &description);
        append_packet(&mut utf8, TYPE_IFSC, &ifsc(utf8_id, 1, 30));
        assert_eq!(
            parse_recovery_set(&utf8).unwrap().files[&utf8_id].relative_path,
            "Série/Épisode.mkv"
        );

        let (corpus, selected, unrelated) = fixture(2);
        let mut case_distinct = corpus.clone();
        append_packet(
            &mut case_distinct,
            TYPE_UNICODE_FILENAME,
            &unicode_filename(selected, "same/Movie.mkv"),
        );
        append_packet(
            &mut case_distinct,
            TYPE_UNICODE_FILENAME,
            &unicode_filename(unrelated, "SAME/movie.MKV"),
        );
        assert!(parse_recovery_set(&case_distinct).is_ok());

        let mut collision = corpus;
        append_packet(
            &mut collision,
            TYPE_UNICODE_FILENAME,
            &unicode_filename(selected, "same/Movie.mkv"),
        );
        append_packet(
            &mut collision,
            TYPE_UNICODE_FILENAME,
            &unicode_filename(unrelated, "same/Movie.mkv"),
        );
        assert_eq!(
            parse_recovery_set(&collision),
            Err("par2_file_name_conflict")
        );
    }

    #[test]
    fn repair_rank_counts_unrelated_unknowns_but_prioritizes_the_target() {
        let (corpus, selected, unrelated) = fixture(2);
        let set = parse_recovery_set(&corpus).unwrap();
        let known = BTreeMap::from([
            (selected, vec![true, false]),
            (unrelated, vec![false, true]),
        ]);

        let plan = plan_repair(&set, selected, &known).unwrap();

        assert_eq!(plan.unknown_slices.len(), 2);
        assert_eq!(plan.unknown_slices[0].file_id, selected);
        assert!(plan.unknown_slices[0].selected_asset);
        assert_eq!(plan.unknown_slices[1].file_id, unrelated);
        assert_eq!(plan.recovery_exponents, [0, 1]);
    }

    #[test]
    fn rejects_insufficient_repair_rank() {
        let (corpus, selected, unrelated) = fixture(1);
        let set = parse_recovery_set(&corpus).unwrap();
        assert_eq!(
            plan_repair(
                &set,
                selected,
                &BTreeMap::from([
                    (selected, vec![true, false]),
                    (unrelated, vec![false, true])
                ])
            ),
            Err("repair_insufficient")
        );
    }

    #[test]
    fn salvages_corruption_but_rejects_conflicts_and_catalog_mismatches() {
        let (mut corrupt, _, _) = fixture(2);
        let main_packet_length =
            usize::try_from(u64::from_le_bytes(corrupt[8..16].try_into().unwrap())).unwrap();
        let valid_main = corrupt[..main_packet_length].to_vec();
        corrupt[70] ^= 1;
        corrupt.extend_from_slice(&valid_main);
        let mut prefixed = b"damaged-prefix".to_vec();
        prefixed.extend_from_slice(&corrupt);
        corrupt = prefixed;
        assert!(parse_recovery_set(&corrupt).is_ok());

        let (id, description) = file_description("../escape.mkv", 4, 5);
        let mut main = 4_u64.to_le_bytes().to_vec();
        main.extend_from_slice(&1_u32.to_le_bytes());
        main.extend_from_slice(&id);
        let mut unsafe_corpus = main_packet(&main);
        append_packet(&mut unsafe_corpus, TYPE_FILE_DESCRIPTION, &description);
        append_packet(&mut unsafe_corpus, TYPE_IFSC, &ifsc(id, 1, 2));
        assert_eq!(
            parse_recovery_set(&unsafe_corpus),
            Err("par2_file_description_missing")
        );

        let (safe_id, mut safe_description) = file_description("safe.mkv", 4, 5);
        safe_description[32] ^= 1;
        let mut safe_main = 4_u64.to_le_bytes().to_vec();
        safe_main.extend_from_slice(&1_u32.to_le_bytes());
        safe_main.extend_from_slice(&safe_id);
        let mut mismatch = main_packet(&safe_main);
        append_packet(&mut mismatch, TYPE_FILE_DESCRIPTION, &safe_description);
        append_packet(&mut mismatch, TYPE_IFSC, &ifsc(safe_id, 1, 2));
        assert_eq!(
            parse_recovery_set(&mismatch),
            Err("par2_file_description_missing")
        );

        let (corpus, _, _) = fixture(2);
        let truncated = &corpus[..corpus.len() - 1];
        assert_eq!(
            parse_recovery_set(truncated).unwrap().recovery_exponents,
            [0].into()
        );

        let (mut conflict, _, _) = fixture(1);
        let mut conflicting_slice = 0_u32.to_le_bytes().to_vec();
        conflicting_slice.extend_from_slice(&[9; 4]);
        append_packet(&mut conflict, TYPE_RECOVERY_SLICE, &conflicting_slice);
        assert_eq!(
            parse_recovery_set(&conflict),
            Err("par2_recovery_slice_conflict")
        );

        let (mut foreign_set, _, _) = fixture(1);
        let mut foreign = packet_for_set(
            corpus_set_id(&foreign_set),
            TYPE_RECOVERY_SLICE,
            &[2, 0, 0, 0, 1, 2, 3, 4],
        );
        foreign[32] ^= 1;
        let digest = md5(&foreign[32..]);
        foreign[16..32].copy_from_slice(&digest);
        foreign_set.extend_from_slice(&foreign);
        assert_eq!(
            parse_recovery_set(&foreign_set),
            Err("par2_recovery_set_mismatch")
        );

        let (short_id, short_description) = file_description("short.mkv", 8, 8);
        let mut short_main = 4_u64.to_le_bytes().to_vec();
        short_main.extend_from_slice(&1_u32.to_le_bytes());
        short_main.extend_from_slice(&short_id);
        let mut wrong_slice_count = main_packet(&short_main);
        append_packet(
            &mut wrong_slice_count,
            TYPE_FILE_DESCRIPTION,
            &short_description,
        );
        append_packet(&mut wrong_slice_count, TYPE_IFSC, &ifsc(short_id, 1, 1));
        assert_eq!(
            parse_recovery_set(&wrong_slice_count),
            Err("par2_slice_count_invalid")
        );

        let (mut invalid_exponent, _, _) = fixture(0);
        let mut recovery_slice = u32::from(u16::MAX).to_le_bytes().to_vec();
        recovery_slice.extend_from_slice(&[0; 4]);
        append_packet(&mut invalid_exponent, TYPE_RECOVERY_SLICE, &recovery_slice);
        assert!(
            parse_recovery_set(&invalid_exponent)
                .unwrap()
                .recovery_exponents
                .is_empty()
        );
    }

    #[test]
    fn streams_file_length_hashes_and_every_ifsc_slice() {
        let bytes = b"ABCDEFG";
        let final_checksum = super::padded_slice_checksum(b"EFG", 4);
        assert_eq!(
            final_checksum.md5,
            [
                0xbd, 0xdc, 0xfb, 0xf2, 0xeb, 0x70, 0x71, 0x90, 0x19, 0x26, 0x4d, 0xda, 0x48, 0x97,
                0x60, 0x62,
            ]
        );
        assert_eq!(final_checksum.crc32, 0x46a1_5fa3);
        let name = "verified.bin";
        let first = md5(bytes);
        let id = file_id(first, bytes.len() as u64, name);
        let mut description = Vec::new();
        description.extend_from_slice(&id);
        description.extend_from_slice(&md5(bytes));
        description.extend_from_slice(&first);
        description.extend_from_slice(&(bytes.len() as u64).to_le_bytes());
        description.extend_from_slice(name.as_bytes());
        while !(64 + description.len()).is_multiple_of(4) {
            description.push(0);
        }
        let mut main = 4_u64.to_le_bytes().to_vec();
        main.extend_from_slice(&1_u32.to_le_bytes());
        main.extend_from_slice(&id);
        let mut checksums = id.to_vec();
        for slice in bytes.chunks(4) {
            let checksum = super::padded_slice_checksum(slice, 4);
            checksums.extend_from_slice(&checksum.md5);
            checksums.extend_from_slice(&checksum.crc32.to_le_bytes());
        }
        let mut corpus = main_packet(&main);
        append_packet(&mut corpus, TYPE_FILE_DESCRIPTION, &description);
        append_packet(&mut corpus, TYPE_IFSC, &checksums);
        let set = parse_recovery_set(&corpus).unwrap();

        assert_eq!(
            identify_complete_source(
                &set,
                &mut std::io::Cursor::new(bytes),
                bytes.len() as u64,
                &|| false
            ),
            Ok(id)
        );
        assert_eq!(set.files[&id].relative_path, name);
        assert_eq!(
            verify_complete_source(
                &set,
                id,
                &mut std::io::Cursor::new(bytes),
                bytes.len() as u64,
                &|| false,
            ),
            Ok(())
        );
        let mut corrupt_bytes = bytes.to_vec();
        corrupt_bytes[5] ^= 1;
        assert_eq!(
            identify_complete_source(
                &set,
                &mut std::io::Cursor::new(&corrupt_bytes),
                bytes.len() as u64,
                &|| false
            ),
            Err("par2_source_unmatched")
        );
        assert_eq!(
            verify_complete_source(
                &set,
                id,
                &mut std::io::Cursor::new(&corrupt_bytes),
                bytes.len() as u64,
                &|| false,
            ),
            Err("par2_file_evidence_invalid")
        );
        assert_eq!(
            identify_complete_source(
                &set,
                &mut std::io::Cursor::new(bytes),
                bytes.len() as u64,
                &|| true
            ),
            Err("materialization_cancelled")
        );
        assert_eq!(
            identify_complete_source(&set, &mut std::io::Cursor::new(&bytes[..6]), 6, &|| false,),
            Err("par2_source_unmatched")
        );

        let duplicate_name = "duplicate.bin";
        let duplicate_id = file_id(first, bytes.len() as u64, duplicate_name);
        let mut duplicate_description = Vec::new();
        duplicate_description.extend_from_slice(&duplicate_id);
        duplicate_description.extend_from_slice(&md5(bytes));
        duplicate_description.extend_from_slice(&first);
        duplicate_description.extend_from_slice(&(bytes.len() as u64).to_le_bytes());
        duplicate_description.extend_from_slice(duplicate_name.as_bytes());
        while !(64 + duplicate_description.len()).is_multiple_of(4) {
            duplicate_description.push(0);
        }
        let mut ambiguous_ids = [id, duplicate_id];
        ambiguous_ids.sort();
        let mut ambiguous_main = 4_u64.to_le_bytes().to_vec();
        ambiguous_main.extend_from_slice(&2_u32.to_le_bytes());
        for file_id in ambiguous_ids {
            ambiguous_main.extend_from_slice(&file_id);
        }
        let mut duplicate_checksums = duplicate_id.to_vec();
        for slice in bytes.chunks(4) {
            let checksum = super::padded_slice_checksum(slice, 4);
            duplicate_checksums.extend_from_slice(&checksum.md5);
            duplicate_checksums.extend_from_slice(&checksum.crc32.to_le_bytes());
        }
        let mut ambiguous = main_packet(&ambiguous_main);
        append_packet(&mut ambiguous, TYPE_FILE_DESCRIPTION, &description);
        append_packet(
            &mut ambiguous,
            TYPE_FILE_DESCRIPTION,
            &duplicate_description,
        );
        append_packet(&mut ambiguous, TYPE_IFSC, &checksums);
        append_packet(&mut ambiguous, TYPE_IFSC, &duplicate_checksums);
        let ambiguous = parse_recovery_set(&ambiguous).unwrap();
        assert_eq!(
            identify_complete_source(
                &ambiguous,
                &mut std::io::Cursor::new(bytes),
                bytes.len() as u64,
                &|| false
            ),
            Err("par2_source_ambiguous")
        );
        assert_eq!(
            verify_complete_source(
                &ambiguous,
                duplicate_id,
                &mut std::io::Cursor::new(bytes),
                bytes.len() as u64,
                &|| false,
            ),
            Ok(())
        );
    }

    #[test]
    fn maps_partial_source_from_typed_stage_evidence() {
        let (set, ids) =
            partial_fixture(&[("selected.bin", b"AAAABBBB"), ("other.bin", b"CCCCDDDD")]);
        let selected_checksums = &set.checksums[&ids[0]];
        let mapping = identify_partial_source_evidence(
            &set,
            &PartialSourceEvidence {
                exact_size: 8,
                checksums: vec![Some(selected_checksums[0]), None],
                full_md5: None,
                first_16k_md5: None,
            },
        )
        .unwrap();

        assert_eq!(mapping.file_id, ids[0]);
        assert_eq!(mapping.valid_slices, [true, false]);
        assert!(!mapping.complete);

        let complete = identify_partial_source_evidence(
            &set,
            &PartialSourceEvidence {
                exact_size: 8,
                checksums: selected_checksums.iter().copied().map(Some).collect(),
                full_md5: Some(md5(b"AAAABBBB")),
                first_16k_md5: Some(md5(b"AAAABBBB")),
            },
        )
        .unwrap();
        assert_eq!(complete.file_id, ids[0]);
        assert_eq!(complete.valid_slices, [true, true]);
        assert!(complete.complete);
    }

    #[test]
    fn partial_source_evidence_rejects_ambiguity_and_corruption() {
        let (ambiguous, ids) =
            partial_fixture(&[("first.bin", b"AAAABBBB"), ("second.bin", b"AAAACCCC")]);
        let shared = ambiguous.checksums[&ids[0]][0];
        let evidence = PartialSourceEvidence {
            exact_size: 8,
            checksums: vec![Some(shared), None],
            full_md5: None,
            first_16k_md5: None,
        };
        assert_eq!(
            identify_partial_source_evidence(&ambiguous, &evidence),
            Err("par2_source_ambiguous")
        );

        let (distinct, ids) =
            partial_fixture(&[("first.bin", b"AAAABBBB"), ("second.bin", b"CCCCDDDD")]);
        let mut corrupt = distinct.checksums[&ids[0]][0];
        corrupt.crc32 ^= 1;
        assert_eq!(
            identify_partial_source_evidence(
                &distinct,
                &PartialSourceEvidence {
                    exact_size: 8,
                    checksums: vec![Some(corrupt), None],
                    full_md5: None,
                    first_16k_md5: None,
                },
            ),
            Err("par2_source_unmatched")
        );
    }
}
