use crate::cache::VerifiedSegment;
#[cfg(test)]
use crate::yenc::DecodedPart;
use crc32fast::Hasher as Crc32Hasher;
use md5::Md5;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::ffi::CString;
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::io::{Read, Seek, SeekFrom};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

const MAX_MATERIALIZATION_BYTES: u64 = crate::limits::MAX_LOGICAL_BYTES;
const ASSET_REVISION_DOMAIN: &[u8] = b"comet-asset-v1\0";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ImmutableFileIdentity {
    pub device: u64,
    pub inode: u64,
    pub size: u64,
    pub mode: u32,
    pub links: u64,
    pub modified_seconds: i64,
    pub modified_nanoseconds: i64,
    pub changed_seconds: i64,
    pub changed_nanoseconds: i64,
}

pub struct StagedMaterialization {
    root: PathBuf,
    directory: PathBuf,
    path: PathBuf,
    output: Option<File>,
    extents: BTreeMap<u64, u64>,
    expected_size: Option<u64>,
    expected_whole_crc32: Option<u32>,
}

pub struct ArchiveExtractionStage {
    root: PathBuf,
    directory: PathBuf,
    input: PathBuf,
    output: PathBuf,
}

pub struct Par2RepairStage {
    root: PathBuf,
    directory: PathBuf,
    logical_bytes: u64,
}

pub struct PartialSourceStage {
    directory: PathBuf,
    input: File,
    exact_size: u64,
    extents: BTreeMap<u64, u64>,
    known_bytes: u64,
}

pub struct ImmutableRangeReader {
    input: File,
    expected_size: u64,
    expected_identity: ImmutableFileIdentity,
}

pub(crate) struct VerifiedInput<'a> {
    input: &'a mut File,
    remaining: u64,
    digest: Sha256,
    asset_revision: Option<Sha256>,
}

impl Read for VerifiedInput<'_> {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        if self.remaining == 0 {
            return Ok(0);
        }
        let wanted = self.remaining.min(buffer.len() as u64) as usize;
        let length = self.input.read(&mut buffer[..wanted])?;
        self.digest.update(&buffer[..length]);
        if let Some(asset_revision) = self.asset_revision.as_mut() {
            asset_revision.update(&buffer[..length]);
        }
        self.remaining -= length as u64;
        Ok(length)
    }
}

#[derive(Clone, Copy)]
pub struct VerifiedMaterializationPart<'a> {
    pub content_identity: &'a str,
    pub exact_size: u64,
    pub file_identity: ImmutableFileIdentity,
}

impl Drop for StagedMaterialization {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.directory);
    }
}

impl Drop for ArchiveExtractionStage {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.directory);
    }
}

impl Drop for Par2RepairStage {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.directory);
    }
}

impl Drop for PartialSourceStage {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.directory);
    }
}

fn nonce() -> Result<u128, &'static str> {
    let mut random = [0_u8; 16];
    getrandom::fill(&mut random).map_err(|_| "materialization_unavailable")?;
    Ok(u128::from_be_bytes(random))
}

pub fn cleanup_staging(root: &Path) -> Result<(), &'static str> {
    let staging_root = root.join("staging");
    let entries = match fs::read_dir(&staging_root) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(_) => return Err("materialization_unavailable"),
    };
    for entry in entries {
        let entry = entry.map_err(|_| "materialization_unavailable")?;
        let file_type = entry
            .file_type()
            .map_err(|_| "materialization_unavailable")?;
        let name = entry.file_name();
        let name = name.as_bytes();
        if file_type.is_dir()
            && (name.starts_with(b".stage-")
                || name.starts_with(b".archive-")
                || name.starts_with(b".par2-")
                || name.starts_with(b".partial-"))
        {
            fs::remove_dir_all(entry.path()).map_err(|_| "materialization_unavailable")?;
        }
    }
    Ok(())
}

impl PartialSourceStage {
    pub fn new(root: &Path, exact_size: u64) -> Result<Self, &'static str> {
        let staging_root = root.join("staging");
        secure_directory(&staging_root)?;
        let directory = staging_root.join(format!(".partial-{}-{}", std::process::id(), nonce()?));
        fs::create_dir(&directory).map_err(|_| "materialization_unavailable")?;
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o700))
            .map_err(|_| "materialization_unavailable")?;
        let path = directory.join("source.bin");
        let input = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path)
            .map_err(|_| "materialization_unavailable")?;
        input
            .set_len(exact_size)
            .map_err(|_| "materialization_unavailable")?;
        Ok(Self {
            directory,
            input,
            exact_size,
            extents: BTreeMap::new(),
            known_bytes: 0,
        })
    }

    #[cfg(test)]
    pub fn push(&mut self, part: DecodedPart) -> Result<(), &'static str> {
        let segment =
            VerifiedSegment::from_decoded(part).map_err(|_| "par2_source_evidence_invalid")?;
        self.push_segment(&segment)
    }

    pub fn push_segment(&mut self, segment: &VerifiedSegment) -> Result<(), &'static str> {
        let begin = segment.begin - 1;
        let end = segment.end;
        let bytes = segment.bytes();
        if segment.total_size != self.exact_size
            || self
                .extents
                .range(..end)
                .next_back()
                .is_some_and(|(_start, previous_end)| *previous_end > begin)
        {
            return Err("par2_source_evidence_invalid");
        }
        self.input
            .seek(SeekFrom::Start(begin))
            .and_then(|_| self.input.write_all(bytes))
            .map_err(|_| "materialization_unavailable")?;
        self.extents.insert(begin, end);
        self.known_bytes += bytes.len() as u64;
        Ok(())
    }

    fn covers(&self, start: u64, end: u64) -> bool {
        let mut cursor = start;
        while cursor < end {
            let Some((_begin, extent_end)) = self.extents.range(..=cursor).next_back() else {
                return false;
            };
            if *extent_end <= cursor {
                return false;
            }
            cursor = (*extent_end).min(end);
        }
        true
    }

    pub fn evidence<F>(
        &mut self,
        slice_size: u64,
        cancelled: &F,
    ) -> Result<crate::par2::PartialSourceEvidence, &'static str>
    where
        F: Fn() -> bool,
    {
        if cancelled() {
            return Err("materialization_cancelled");
        }
        self.input
            .flush()
            .map_err(|_| "materialization_unavailable")?;
        let slice_count = self.exact_size.div_ceil(slice_size) as usize;
        let mut checksums = Vec::with_capacity(slice_count);
        let slice_buffer_size = slice_size as usize;
        let mut buffer = vec![0_u8; slice_buffer_size];
        for slice_index in 0..slice_count {
            if cancelled() {
                return Err("materialization_cancelled");
            }
            let start = slice_index as u64 * slice_size;
            let end = (start + slice_size).min(self.exact_size);
            if !self.covers(start, end) {
                checksums.push(None);
                continue;
            }
            let length = (end - start) as usize;
            self.input
                .seek(SeekFrom::Start(start))
                .and_then(|_| self.input.read_exact(&mut buffer[..length]))
                .map_err(|_| "materialization_unavailable")?;
            checksums.push(Some(crate::par2::padded_slice_checksum(
                &buffer[..length],
                slice_buffer_size,
            )));
        }
        let complete = self.known_bytes == self.exact_size && self.covers(0, self.exact_size);
        let (full_md5, first_16k_md5) = if complete {
            self.input
                .seek(SeekFrom::Start(0))
                .map_err(|_| "materialization_unavailable")?;
            let mut full = Md5::new();
            let mut first = Md5::new();
            let mut processed = 0u64;
            let mut buffer = [0u8; 64 * 1024];
            while processed < self.exact_size {
                if cancelled() {
                    return Err("materialization_cancelled");
                }
                let wanted = (self.exact_size - processed).min(buffer.len() as u64) as usize;
                let length = self
                    .input
                    .read(&mut buffer[..wanted])
                    .map_err(|_| "materialization_unavailable")?;
                if length == 0 {
                    return Err("par2_source_evidence_invalid");
                }
                full.update(&buffer[..length]);
                if processed < 16 * 1024 {
                    let first_length = (16 * 1024_u64 - processed).min(length as u64) as usize;
                    first.update(&buffer[..first_length]);
                }
                processed += length as u64;
            }
            (
                Some(<[u8; 16]>::from(full.finalize())),
                Some(<[u8; 16]>::from(first.finalize())),
            )
        } else {
            (None, None)
        };
        Ok(crate::par2::PartialSourceEvidence {
            exact_size: self.exact_size,
            checksums,
            full_md5,
            first_16k_md5,
        })
    }
}

impl Par2RepairStage {
    #[cfg(test)]
    pub fn new(root: &Path) -> Result<Self, &'static str> {
        Self::new_with_publication(root, root)
    }

    pub fn new_with_publication(
        staging_data: &Path,
        publication_data: &Path,
    ) -> Result<Self, &'static str> {
        let staging_root = staging_data.join("staging");
        secure_directory(&staging_root)?;
        let directory = staging_root.join(format!(".par2-{}-{}", std::process::id(), nonce()?));
        fs::create_dir(&directory).map_err(|_| "materialization_unavailable")?;
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o700))
            .map_err(|_| "materialization_unavailable")?;
        Ok(Self {
            root: publication_data.to_path_buf(),
            directory,
            logical_bytes: 0,
        })
    }

    fn source_path(&self, relative_path: &str) -> Result<PathBuf, &'static str> {
        let normalized = crate::archive::normalize_archive_path(relative_path)
            .map_err(|_| "par2_file_name_invalid")?;
        if normalized != relative_path || normalized.eq_ignore_ascii_case("index.par2") {
            return Err("repair_stage_path_conflict");
        }
        let mut current = self.directory.clone();
        if let Some(parent) = Path::new(&normalized).parent() {
            for component in parent.components() {
                current.push(component.as_os_str());
                match fs::create_dir(&current) {
                    Ok(()) => fs::set_permissions(&current, fs::Permissions::from_mode(0o700))
                        .map_err(|_| "materialization_unavailable")?,
                    Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                        let metadata = fs::symlink_metadata(&current)
                            .map_err(|_| "materialization_unavailable")?;
                        if !metadata.file_type().is_dir()
                            || metadata.permissions().mode() & 0o777 != 0o700
                        {
                            return Err("repair_stage_invalid");
                        }
                    }
                    Err(_) => return Err("materialization_unavailable"),
                }
            }
        }
        Ok(self.directory.join(normalized))
    }

    pub fn create_source(
        &mut self,
        relative_path: &str,
        exact_size: u64,
    ) -> Result<(), &'static str> {
        let path = self.source_path(relative_path)?;
        let output = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&path)
            .map_err(|_| "materialization_unavailable")?;
        output
            .set_len(exact_size)
            .map_err(|_| "materialization_unavailable")?;
        self.logical_bytes += exact_size;
        Ok(())
    }

    pub fn adopt_partial_source(
        &mut self,
        partial: &PartialSourceStage,
        relative_path: &str,
    ) -> Result<(), &'static str> {
        let metadata = partial
            .input
            .metadata()
            .map_err(|_| "materialization_unavailable")?;
        if !metadata.file_type().is_file()
            || metadata.len() != partial.exact_size
            || metadata.permissions().mode() & 0o777 != 0o600
            || metadata.nlink() != 1
        {
            return Err("repair_stage_invalid");
        }
        fs::rename(
            partial.directory.join("source.bin"),
            self.source_path(relative_path)?,
        )
        .map_err(|_| "materialization_unavailable")?;
        self.logical_bytes += partial.exact_size;
        Ok(())
    }

    pub fn copy_complete_source<F>(
        &self,
        content_identity: &str,
        exact_size: u64,
        relative_path: &str,
        cancelled: &F,
    ) -> Result<(), &'static str>
    where
        F: Fn() -> bool,
    {
        let target = self.source_path(relative_path)?;
        let mut output = OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
            .open(&target)
            .map_err(|_| "materialization_unavailable")?;
        let metadata = output
            .metadata()
            .map_err(|_| "materialization_unavailable")?;
        if !metadata.file_type().is_file()
            || metadata.len() != exact_size
            || metadata.permissions().mode() & 0o777 != 0o600
            || metadata.nlink() != 1
        {
            return Err("repair_stage_invalid");
        }
        output
            .seek(SeekFrom::Start(0))
            .map_err(|_| "materialization_unavailable")?;
        inspect_verified_cancellable(
            &self.root,
            content_identity,
            exact_size,
            cancelled,
            |input| {
                let mut remaining = exact_size;
                let mut buffer = [0u8; 64 * 1024];
                while remaining != 0 {
                    if cancelled() {
                        return Err("materialization_cancelled");
                    }
                    let wanted = remaining.min(buffer.len() as u64) as usize;
                    let length = input
                        .read(&mut buffer[..wanted])
                        .map_err(|_| "materialization_unavailable")?;
                    if length == 0 {
                        return Err("materialization_conflict");
                    }
                    output
                        .write_all(&buffer[..length])
                        .map_err(|_| "materialization_unavailable")?;
                    remaining -= length as u64;
                }
                Ok(())
            },
        )?;
        Ok(())
    }

    pub fn copy_partial_source<F>(
        &self,
        partial: &mut PartialSourceStage,
        relative_path: &str,
        valid_slices: &[bool],
        slice_size: u64,
        cancelled: &F,
    ) -> Result<(), &'static str>
    where
        F: Fn() -> bool,
    {
        let target = self.source_path(relative_path)?;
        let mut output = OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
            .open(&target)
            .map_err(|_| "materialization_unavailable")?;
        let metadata = output
            .metadata()
            .map_err(|_| "materialization_unavailable")?;
        if !metadata.file_type().is_file()
            || metadata.len() != partial.exact_size
            || metadata.permissions().mode() & 0o777 != 0o600
            || metadata.nlink() != 1
        {
            return Err("repair_stage_invalid");
        }
        let partial_metadata = partial
            .input
            .metadata()
            .map_err(|_| "materialization_unavailable")?;
        if !partial_metadata.file_type().is_file()
            || partial_metadata.len() != partial.exact_size
            || partial_metadata.permissions().mode() & 0o777 != 0o600
            || partial_metadata.nlink() != 1
        {
            return Err("repair_stage_invalid");
        }
        let mut buffer = [0u8; 64 * 1024];
        for (slice_index, valid) in valid_slices.iter().enumerate() {
            if !valid {
                continue;
            }
            let start = slice_index as u64 * slice_size;
            let end = (start + slice_size).min(partial.exact_size);
            partial
                .input
                .seek(SeekFrom::Start(start))
                .and_then(|_| output.seek(SeekFrom::Start(start)))
                .map_err(|_| "materialization_unavailable")?;
            let mut remaining = end - start;
            while remaining != 0 {
                if cancelled() {
                    return Err("materialization_cancelled");
                }
                let wanted = remaining.min(buffer.len() as u64) as usize;
                partial
                    .input
                    .read_exact(&mut buffer[..wanted])
                    .and_then(|_| output.write_all(&buffer[..wanted]))
                    .map_err(|_| "materialization_unavailable")?;
                remaining -= wanted as u64;
            }
        }
        Ok(())
    }

    pub fn write_index_ranges<F>(
        &mut self,
        volumes: &mut [File],
        ranges: &[Vec<(u64, u64)>],
        cancelled: &F,
    ) -> Result<(), &'static str>
    where
        F: Fn() -> bool,
    {
        assert_eq!(
            volumes.len(),
            ranges.len(),
            "PAR2 readers and packet ranges must correspond"
        );
        let input_bytes = ranges
            .iter()
            .flatten()
            .map(|(_offset, length)| length)
            .sum();
        self.write_index_data(input_bytes, |output| {
            for (input, ranges) in volumes.iter_mut().zip(ranges) {
                for (offset, length) in ranges {
                    if cancelled() {
                        return Err("materialization_cancelled");
                    }
                    input
                        .seek(SeekFrom::Start(*offset))
                        .map_err(|_| "materialization_unavailable")?;
                    let copied = std::io::copy(&mut input.take(*length), output)
                        .map_err(|_| "materialization_unavailable")?;
                    if copied != *length {
                        return Err("materialization_unavailable");
                    }
                }
            }
            Ok(())
        })
    }

    fn write_index_data<W>(&mut self, input_bytes: u64, write: W) -> Result<(), &'static str>
    where
        W: FnOnce(&mut File) -> Result<(), &'static str>,
    {
        let index = self.directory.join("index.par2");
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(index)
            .map_err(|_| "materialization_unavailable")?;
        write(&mut output)?;
        self.logical_bytes += input_bytes;
        Ok(())
    }

    pub fn directory(&self) -> &Path {
        &self.directory
    }

    pub fn logical_bytes(&self) -> u64 {
        self.logical_bytes
    }

    pub fn publish_verified_selected<T, I>(
        &self,
        relative_path: &str,
        expected_size: u64,
        inspect: I,
    ) -> Result<(PathBuf, String, u64, String, T), &'static str>
    where
        I: FnOnce(&mut File) -> Result<T, &'static str>,
    {
        let source = self.source_path(relative_path)?;
        let mut input = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
            .open(&source)
            .map_err(|_| "materialization_unavailable")?;
        let metadata = input
            .metadata()
            .map_err(|_| "materialization_unavailable")?;
        if !metadata.file_type().is_file()
            || metadata.len() != expected_size
            || metadata.permissions().mode() & 0o777 != 0o600
            || metadata.nlink() != 1
        {
            return Err("par2_file_evidence_invalid");
        }
        let initial_identity = immutable_file_identity(&metadata);
        let inspected = inspect(&mut input)?;
        let revalidated = input
            .metadata()
            .map_err(|_| "materialization_unavailable")?;
        if immutable_file_identity(&revalidated) != initial_identity {
            return Err("materialization_conflict");
        }
        input
            .set_permissions(fs::Permissions::from_mode(0o400))
            .map_err(|_| "materialization_unavailable")?;
        drop(input);
        let (path, identity, size, asset_revision) = publish_immutable_file(&self.root, &source)?;
        Ok((path, identity, size, asset_revision, inspected))
    }
}

pub(crate) fn verified_reader_cancellable<F>(
    root: &Path,
    identity: &str,
    expected_size: u64,
    cancelled: &F,
) -> Result<File, &'static str>
where
    F: Fn() -> bool,
{
    let target = path(root, identity);
    let (mut input, _identity, _asset_revision) =
        open_verified_cancellable(&target, identity, expected_size, None, cancelled, false)?;
    input
        .seek(SeekFrom::Start(0))
        .map_err(|_| "materialization_unavailable")?;
    Ok(input)
}

impl StagedMaterialization {
    #[cfg(test)]
    pub fn new(root: &Path) -> Result<Self, &'static str> {
        Self::new_with_publication(root, root)
    }

    pub fn new_with_publication(
        staging_data: &Path,
        publication_data: &Path,
    ) -> Result<Self, &'static str> {
        let staging_root = staging_data.join("staging");
        secure_directory(&staging_root)?;
        let directory = staging_root.join(format!(".stage-{}-{}", std::process::id(), nonce()?));
        fs::create_dir(&directory).map_err(|_| "materialization_unavailable")?;
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o700))
            .map_err(|_| "materialization_unavailable")?;
        let path = directory.join("output.bin");
        let output = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&path)
            .map_err(|_| "materialization_unavailable")?;
        Ok(Self {
            root: publication_data.to_path_buf(),
            directory,
            path,
            output: Some(output),
            extents: BTreeMap::new(),
            expected_size: None,
            expected_whole_crc32: None,
        })
    }

    pub fn push_segment(&mut self, segment: &VerifiedSegment) -> Result<(), &'static str> {
        let begin = segment.begin - 1;
        let end = segment.end;
        if self
            .expected_size
            .is_some_and(|expected| expected != segment.total_size)
            || self
                .extents
                .range(..end)
                .next_back()
                .is_some_and(|(_start, previous_end)| *previous_end > begin)
        {
            return Err("materialization_gap");
        }
        if let Some(value) = segment.whole_crc32
            && self
                .expected_whole_crc32
                .replace(value)
                .is_some_and(|expected| expected != value)
        {
            return Err("materialization_checksum_mismatch");
        }
        let output = self
            .output
            .as_mut()
            .expect("materialization output remains open before publication");
        if self.expected_size.is_none() {
            output
                .set_len(segment.total_size)
                .map_err(|_| "materialization_unavailable")?;
            self.expected_size = Some(segment.total_size);
        }
        output
            .seek(SeekFrom::Start(begin))
            .map_err(|_| "materialization_unavailable")?;
        output
            .write_all(segment.bytes())
            .map_err(|_| "materialization_unavailable")?;
        self.extents.insert(begin, end);
        Ok(())
    }

    pub fn publish(&mut self) -> Result<(PathBuf, String, u64, String), &'static str> {
        let expected_size = self
            .expected_size
            .expect("materialization stage contains its first fetched segment");
        let mut next = 0u64;
        for (begin, end) in &self.extents {
            if *begin != next {
                return Err("materialization_gap");
            }
            next = *end;
        }
        if next != expected_size {
            return Err("materialization_size_mismatch");
        }
        let mut output = self
            .output
            .take()
            .expect("materialization output is published once");
        output
            .flush()
            .and_then(|_| output.seek(SeekFrom::Start(0)).map(|_| ()))
            .map_err(|_| "materialization_unavailable")?;
        let metadata = output
            .metadata()
            .map_err(|_| "materialization_unavailable")?;
        if !metadata.file_type().is_file()
            || metadata.len() != expected_size
            || metadata.permissions().mode() & 0o777 != 0o600
            || metadata.nlink() != 1
        {
            return Err("materialization_conflict");
        }
        let initial_identity = immutable_file_identity(&metadata);
        let mut digest = Sha256::new();
        let mut asset_revision = asset_revision_hasher(expected_size);
        let mut crc32 = self.expected_whole_crc32.map(|_| Crc32Hasher::new());
        let mut buffer = [0u8; 64 * 1024];
        loop {
            let length = output
                .read(&mut buffer)
                .map_err(|_| "materialization_unavailable")?;
            if length == 0 {
                break;
            }
            digest.update(&buffer[..length]);
            asset_revision.update(&buffer[..length]);
            if let Some(crc32) = crc32.as_mut() {
                crc32.update(&buffer[..length]);
            }
        }
        let revalidated = output
            .metadata()
            .map_err(|_| "materialization_unavailable")?;
        if immutable_file_identity(&revalidated) != initial_identity {
            return Err("materialization_conflict");
        }
        if self
            .expected_whole_crc32
            .zip(crc32)
            .is_some_and(|(expected, actual)| actual.finalize() != expected)
        {
            return Err("materialization_checksum_mismatch");
        }
        output
            .set_permissions(fs::Permissions::from_mode(0o400))
            .and_then(|_| output.sync_all())
            .map_err(|_| "materialization_unavailable")?;
        let identity = format!("{:x}", digest.finalize());
        let asset_revision = format!("{:x}", asset_revision.finalize());
        drop(output);
        let target = publish_target(&self.root, &self.path, &identity, expected_size)?;
        Ok((target, identity, expected_size, asset_revision))
    }
}

impl ArchiveExtractionStage {
    pub fn new(root: &Path) -> Result<Self, &'static str> {
        Self::new_with_publication(root, root)
    }

    pub fn new_with_publication(
        staging_data: &Path,
        publication_data: &Path,
    ) -> Result<Self, &'static str> {
        let staging_root = staging_data.join("staging");
        secure_directory(&staging_root)?;
        let directory = staging_root.join(format!(".archive-{}-{}", std::process::id(), nonce()?));
        fs::create_dir(&directory).map_err(|_| "materialization_unavailable")?;
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o700))
            .map_err(|_| "materialization_unavailable")?;
        let input = directory.join("combined.archive");
        let output = directory.join("selected.bin");
        Ok(Self {
            root: publication_data.to_path_buf(),
            directory,
            input,
            output,
        })
    }

    pub fn concatenate_verified<F>(
        &self,
        parts: &[VerifiedMaterializationPart<'_>],
        expected_size: u64,
        cancelled: &F,
    ) -> Result<&Path, &'static str>
    where
        F: Fn() -> bool,
    {
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&self.input)
            .map_err(|_| "materialization_unavailable")?;
        let result = (|| {
            let mut buffer = [0_u8; 64 * 1024];
            for part in parts {
                if cancelled() {
                    return Err("materialization_cancelled");
                }
                let mut input = open_nofollow(&path(&self.root, part.content_identity))?;
                let metadata = input
                    .metadata()
                    .map_err(|_| "materialization_unavailable")?;
                if !sealed_file_metadata(&metadata, part.exact_size)
                    || immutable_file_identity(&metadata) != part.file_identity
                {
                    return Err("materialization_conflict");
                }
                let mut remaining = part.exact_size;
                while remaining != 0 {
                    if cancelled() {
                        return Err("materialization_cancelled");
                    }
                    let wanted = remaining.min(buffer.len() as u64) as usize;
                    let read = input
                        .read(&mut buffer[..wanted])
                        .map_err(|_| "materialization_unavailable")?;
                    if read == 0 {
                        return Err("materialization_conflict");
                    }
                    output
                        .write_all(&buffer[..read])
                        .map_err(|_| "materialization_unavailable")?;
                    remaining -= read as u64;
                }
                let revalidated = input
                    .metadata()
                    .map_err(|_| "materialization_unavailable")?;
                if immutable_file_identity(&revalidated) != part.file_identity {
                    return Err("materialization_conflict");
                }
            }
            output.flush().map_err(|_| "materialization_unavailable")?;
            fs::set_permissions(&self.input, fs::Permissions::from_mode(0o400))
                .map_err(|_| "materialization_unavailable")?;
            let metadata =
                fs::symlink_metadata(&self.input).map_err(|_| "materialization_unavailable")?;
            if !sealed_file_metadata(&metadata, expected_size) {
                return Err("materialization_conflict");
            }
            Ok(())
        })();
        if let Err(error) = result {
            drop(output);
            remove_file_if_present(&self.input)?;
            return Err(error);
        }
        Ok(&self.input)
    }

    pub fn output(&self) -> &Path {
        &self.output
    }

    pub fn publish(&self) -> Result<(PathBuf, String, u64, String), &'static str> {
        publish_immutable_file(&self.root, &self.output)
    }
}

#[cfg(test)]
fn valid_identity(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn path(root: &Path, identity: &str) -> PathBuf {
    root.join("materialized").join(format!("{identity}.bin"))
}

fn secure_directory(directory: &Path) -> Result<(), &'static str> {
    fs::create_dir_all(directory).map_err(|_| "materialization_unavailable")?;
    let metadata = fs::symlink_metadata(directory).map_err(|_| "materialization_unavailable")?;
    if !metadata.file_type().is_dir() {
        return Err("materialization_unavailable");
    }
    if metadata.permissions().mode() & 0o777 != 0o700 {
        fs::set_permissions(directory, fs::Permissions::from_mode(0o700))
            .map_err(|_| "materialization_unavailable")?;
    }
    Ok(())
}

fn materialized_directory(root: &Path) -> Result<PathBuf, &'static str> {
    let directory = root.join("materialized");
    secure_directory(&directory)?;
    Ok(directory)
}

fn open_nofollow(path: &Path) -> Result<File, &'static str> {
    OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
        .open(path)
        .map_err(|_| "materialization_unavailable")
}

fn remove_file_if_present(path: &Path) -> Result<(), &'static str> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(_) => Err("materialization_unavailable"),
    }
}

pub(crate) fn immutable_file_identity(metadata: &fs::Metadata) -> ImmutableFileIdentity {
    ImmutableFileIdentity {
        device: metadata.dev(),
        inode: metadata.ino(),
        size: metadata.len(),
        mode: metadata.mode(),
        links: metadata.nlink(),
        modified_seconds: metadata.mtime(),
        modified_nanoseconds: metadata.mtime_nsec(),
        changed_seconds: metadata.ctime(),
        changed_nanoseconds: metadata.ctime_nsec(),
    }
}

pub(crate) fn sealed_file_metadata(metadata: &fs::Metadata, expected_size: u64) -> bool {
    metadata.file_type().is_file()
        && metadata.len() == expected_size
        && metadata.permissions().mode() & 0o777 == 0o400
        && metadata.nlink() == 1
}

pub(crate) fn sealed_identity(
    path: &Path,
    expected_size: u64,
) -> Result<ImmutableFileIdentity, &'static str> {
    let input = open_nofollow(path)?;
    let metadata = input
        .metadata()
        .map_err(|_| "materialization_unavailable")?;
    sealed_file_metadata(&metadata, expected_size)
        .then(|| immutable_file_identity(&metadata))
        .ok_or("materialization_conflict")
}

pub(crate) fn asset_revision_hasher(logical_length: u64) -> Sha256 {
    let mut digest = Sha256::new();
    digest.update(ASSET_REVISION_DOMAIN);
    digest.update(logical_length.to_be_bytes());
    digest
}

fn verify(path: &Path, identity: &str, expected_size: u64) -> Result<(), &'static str> {
    let mut input = open_nofollow(path)?;
    let metadata = input
        .metadata()
        .map_err(|_| "materialization_unavailable")?;
    if !sealed_file_metadata(&metadata, expected_size) {
        return Err("materialization_conflict");
    }
    let initial_identity = immutable_file_identity(&metadata);
    let mut digest = Sha256::new();
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let length = input
            .read(&mut buffer)
            .map_err(|_| "materialization_unavailable")?;
        if length == 0 {
            break;
        }
        digest.update(&buffer[..length]);
    }
    if format!("{:x}", digest.finalize()) != identity {
        return Err("materialization_conflict");
    }
    let revalidated = input
        .metadata()
        .map_err(|_| "materialization_unavailable")?;
    if !sealed_file_metadata(&revalidated, expected_size)
        || immutable_file_identity(&revalidated) != initial_identity
    {
        return Err("materialization_conflict");
    }
    Ok(())
}

fn open_verified_cancellable<F>(
    path: &Path,
    identity: &str,
    expected_size: u64,
    trusted_identity: Option<ImmutableFileIdentity>,
    cancelled: &F,
    include_asset_revision: bool,
) -> Result<(File, ImmutableFileIdentity, Option<String>), &'static str>
where
    F: Fn() -> bool,
{
    if cancelled() {
        return Err("materialization_cancelled");
    }
    let mut input = open_nofollow(path)?;
    let metadata = input
        .metadata()
        .map_err(|_| "materialization_unavailable")?;
    if !sealed_file_metadata(&metadata, expected_size) {
        return Err("materialization_conflict");
    }
    let initial_identity = immutable_file_identity(&metadata);
    if trusted_identity == Some(initial_identity) && !include_asset_revision {
        return Ok((input, initial_identity, None));
    }
    let mut digest = Sha256::new();
    let mut asset_revision = include_asset_revision.then(|| asset_revision_hasher(expected_size));
    let mut buffer = [0u8; 64 * 1024];
    loop {
        if cancelled() {
            return Err("materialization_cancelled");
        }
        let length = input
            .read(&mut buffer)
            .map_err(|_| "materialization_unavailable")?;
        if length == 0 {
            break;
        }
        digest.update(&buffer[..length]);
        if let Some(asset_revision) = asset_revision.as_mut() {
            asset_revision.update(&buffer[..length]);
        }
    }
    if format!("{:x}", digest.finalize()) != identity {
        return Err("materialization_conflict");
    }
    let revalidated = input
        .metadata()
        .map_err(|_| "materialization_unavailable")?;
    if !sealed_file_metadata(&revalidated, expected_size)
        || immutable_file_identity(&revalidated) != initial_identity
    {
        return Err("materialization_conflict");
    }
    Ok((
        input,
        initial_identity,
        asset_revision.map(|digest| format!("{:x}", digest.finalize())),
    ))
}

fn publish_target(
    root: &Path,
    source: &Path,
    identity: &str,
    expected_size: u64,
) -> Result<PathBuf, &'static str> {
    let directory = materialized_directory(root)?;
    let target = path(root, identity);
    match rename_noreplace(source, &target) {
        Ok(()) => {
            File::open(&directory)
                .and_then(|directory| directory.sync_all())
                .map_err(|_| "materialization_unavailable")?;
            Ok(target)
        }
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            verify(&target, identity, expected_size)?;
            Ok(target)
        }
        Err(error) if error.raw_os_error() == Some(libc::EXDEV) => {
            copy_publish_target(source, &directory, &target, identity, expected_size)
        }
        Err(_) => Err("materialization_unavailable"),
    }
}

fn rename_noreplace(source: &Path, target: &Path) -> std::io::Result<()> {
    let source = CString::new(source.as_os_str().as_bytes())
        .map_err(|_| std::io::Error::from_raw_os_error(libc::EINVAL))?;
    let target = CString::new(target.as_os_str().as_bytes())
        .map_err(|_| std::io::Error::from_raw_os_error(libc::EINVAL))?;
    // SAFETY: both paths are live NUL-terminated byte strings. The flags
    // prohibit replacement, so a concurrent winner remains authoritative.
    if unsafe {
        libc::renameat2(
            libc::AT_FDCWD,
            source.as_ptr(),
            libc::AT_FDCWD,
            target.as_ptr(),
            libc::RENAME_NOREPLACE,
        )
    } == 0
    {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

fn copy_publish_target(
    source: &Path,
    directory: &Path,
    target: &Path,
    identity: &str,
    expected_size: u64,
) -> Result<PathBuf, &'static str> {
    let temporary = directory.join(format!(".publish-{}-{}.tmp", std::process::id(), nonce()?));
    let result = (|| {
        let mut input = open_nofollow(source)?;
        let metadata = input
            .metadata()
            .map_err(|_| "materialization_unavailable")?;
        if !sealed_file_metadata(&metadata, expected_size) {
            return Err("materialization_conflict");
        }
        let source_identity = immutable_file_identity(&metadata);
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)
            .map_err(|_| "materialization_unavailable")?;
        let copied =
            std::io::copy(&mut input, &mut output).map_err(|_| "materialization_unavailable")?;
        if copied != expected_size {
            return Err("materialization_conflict");
        }
        let revalidated = input
            .metadata()
            .map_err(|_| "materialization_unavailable")?;
        if immutable_file_identity(&revalidated) != source_identity {
            return Err("materialization_conflict");
        }
        output
            .flush()
            .and_then(|_| output.sync_all())
            .map_err(|_| "materialization_unavailable")?;
        fs::set_permissions(&temporary, fs::Permissions::from_mode(0o400))
            .map_err(|_| "materialization_unavailable")?;
        match rename_noreplace(&temporary, target) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                verify(target, identity, expected_size)?;
            }
            Err(_) => return Err("materialization_unavailable"),
        }
        File::open(directory)
            .and_then(|directory| directory.sync_all())
            .map_err(|_| "materialization_unavailable")?;
        Ok(target.to_path_buf())
    })();
    remove_file_if_present(&temporary)?;
    result
}

fn publish_immutable_file(
    root: &Path,
    source: &Path,
) -> Result<(PathBuf, String, u64, String), &'static str> {
    let mut input = open_nofollow(source)?;
    let metadata = input
        .metadata()
        .map_err(|_| "materialization_unavailable")?;
    let size = metadata.len();
    if size == 0 || size > MAX_MATERIALIZATION_BYTES || !sealed_file_metadata(&metadata, size) {
        return Err("materialization_conflict");
    }
    let initial_identity = immutable_file_identity(&metadata);
    input
        .sync_all()
        .map_err(|_| "materialization_unavailable")?;
    let mut digest = Sha256::new();
    let mut asset_revision = asset_revision_hasher(size);
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let length = input
            .read(&mut buffer)
            .map_err(|_| "materialization_unavailable")?;
        if length == 0 {
            break;
        }
        digest.update(&buffer[..length]);
        asset_revision.update(&buffer[..length]);
    }
    let revalidated = input
        .metadata()
        .map_err(|_| "materialization_unavailable")?;
    if !sealed_file_metadata(&revalidated, size)
        || immutable_file_identity(&revalidated) != initial_identity
    {
        return Err("materialization_conflict");
    }
    let identity = format!("{:x}", digest.finalize());
    let asset_revision = format!("{:x}", asset_revision.finalize());
    let target = publish_target(root, source, &identity, size)?;
    Ok((target, identity, size, asset_revision))
}

#[cfg(test)]
pub fn verified_path(
    root: &Path,
    identity: &str,
    expected_size: u64,
) -> Result<PathBuf, &'static str> {
    if !valid_identity(identity) || expected_size == 0 || expected_size > MAX_MATERIALIZATION_BYTES
    {
        return Err("invalid_materialization");
    }
    let target = path(root, identity);
    drop(open_verified_cancellable(
        &target,
        identity,
        expected_size,
        None,
        &|| false,
        false,
    )?);
    Ok(target)
}

#[cfg(test)]
pub fn publish(
    root: &Path,
    identity: &str,
    mut parts: Vec<DecodedPart>,
    expected_size: u64,
) -> Result<PathBuf, &'static str> {
    if !valid_identity(identity)
        || expected_size == 0
        || expected_size > MAX_MATERIALIZATION_BYTES
        || parts.is_empty()
    {
        return Err("invalid_materialization");
    }
    parts.sort_by_key(|part| part.begin);
    let mut next = 1u64;
    let mut size = 0u64;
    let mut whole_crc32 = None;
    for part in &parts {
        let length = u64::try_from(part.bytes.len()).map_err(|_| "invalid_materialization")?;
        if part.expected_crc32.is_none()
            || part.total_size != expected_size
            || part.begin != next
            || part
                .end
                .checked_sub(part.begin)
                .and_then(|value| value.checked_add(1))
                != Some(length)
        {
            return Err("materialization_gap");
        }
        if let Some(value) = part.expected_whole_crc32
            && whole_crc32
                .replace(value)
                .is_some_and(|expected| expected != value)
        {
            return Err("materialization_checksum_mismatch");
        }
        next = part.end.checked_add(1).ok_or("materialization_gap")?;
        size = size.checked_add(length).ok_or("invalid_materialization")?;
    }
    if size != expected_size {
        return Err("materialization_size_mismatch");
    }
    let directory = materialized_directory(root)?;
    let target = path(root, identity);
    let temporary = directory.join(format!(".{identity}.{}.tmp", std::process::id()));
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)
        .map_err(|_| "materialization_unavailable")?;
    let mut digest = Sha256::new();
    let mut crc32 = Crc32Hasher::new();
    let result = (|| {
        for part in parts {
            output
                .write_all(&part.bytes)
                .map_err(|_| "materialization_unavailable")?;
            digest.update(&part.bytes);
            crc32.update(&part.bytes);
        }
        output.flush().map_err(|_| "materialization_unavailable")?;
        output
            .sync_all()
            .map_err(|_| "materialization_unavailable")?;
        if format!("{:x}", digest.finalize()) != identity {
            return Err("materialization_identity_mismatch");
        }
        if whole_crc32.is_some_and(|expected| crc32.finalize() != expected) {
            return Err("materialization_checksum_mismatch");
        }
        fs::set_permissions(&temporary, fs::Permissions::from_mode(0o400))
            .map_err(|_| "materialization_unavailable")?;
        publish_target(root, &temporary, identity, expected_size).map(|_| ())
    })();
    remove_file_if_present(&temporary)?;
    result?;
    Ok(target)
}

#[cfg(test)]
pub fn publish_generated(
    root: &Path,
    parts: Vec<DecodedPart>,
    expected_size: u64,
) -> Result<(PathBuf, String), &'static str> {
    let mut digest = Sha256::new();
    for part in &parts {
        digest.update(&part.bytes);
    }
    let identity = format!("{:x}", digest.finalize());
    let path = publish(root, &identity, parts, expected_size)?;
    Ok((path, identity))
}

#[cfg(test)]
pub fn publish_generated_auto(
    root: &Path,
    parts: Vec<DecodedPart>,
) -> Result<(PathBuf, String, u64), &'static str> {
    let expected_size = parts.first().ok_or("invalid_materialization")?.total_size;
    let (path, identity) = publish_generated(root, parts, expected_size)?;
    Ok((path, identity, expected_size))
}

pub(crate) fn inspect_verified_cancellable<T, F, I>(
    root: &Path,
    identity: &str,
    expected_size: u64,
    cancelled: &F,
    inspect: I,
) -> Result<T, &'static str>
where
    F: Fn() -> bool,
    I: FnOnce(&mut VerifiedInput<'_>) -> Result<T, &'static str>,
{
    inspect_verified_inner(root, identity, expected_size, cancelled, false, inspect)
        .map(|(result, _asset_revision, _file_identity)| result)
}

pub(crate) fn inspect_verified_with_identity_cancellable<T, F, I>(
    root: &Path,
    identity: &str,
    expected_size: u64,
    cancelled: &F,
    inspect: I,
) -> Result<(T, ImmutableFileIdentity), &'static str>
where
    F: Fn() -> bool,
    I: FnOnce(&mut VerifiedInput<'_>) -> Result<T, &'static str>,
{
    inspect_verified_inner(root, identity, expected_size, cancelled, false, inspect)
        .map(|(result, _asset_revision, file_identity)| (result, file_identity))
}

pub(crate) fn inspect_verified_with_revision_cancellable<T, F, I>(
    root: &Path,
    identity: &str,
    expected_size: u64,
    cancelled: &F,
    inspect: I,
) -> Result<(T, String), &'static str>
where
    F: Fn() -> bool,
    I: FnOnce(&mut VerifiedInput<'_>) -> Result<T, &'static str>,
{
    let (result, asset_revision, _file_identity) =
        inspect_verified_inner(root, identity, expected_size, cancelled, true, inspect)?;
    Ok((
        result,
        asset_revision.expect("asset revision requested from verified input"),
    ))
}

fn inspect_verified_inner<T, F, I>(
    root: &Path,
    identity: &str,
    expected_size: u64,
    cancelled: &F,
    include_asset_revision: bool,
    inspect: I,
) -> Result<(T, Option<String>, ImmutableFileIdentity), &'static str>
where
    F: Fn() -> bool,
    I: FnOnce(&mut VerifiedInput<'_>) -> Result<T, &'static str>,
{
    if expected_size == 0 || expected_size > MAX_MATERIALIZATION_BYTES {
        return Err("invalid_materialization");
    }
    let target = path(root, identity);
    if cancelled() {
        return Err("materialization_cancelled");
    }
    let mut input = open_nofollow(&target)?;
    let metadata = input
        .metadata()
        .map_err(|_| "materialization_unavailable")?;
    if !sealed_file_metadata(&metadata, expected_size) {
        return Err("materialization_conflict");
    }
    let initial_identity = immutable_file_identity(&metadata);
    let mut verified = VerifiedInput {
        input: &mut input,
        remaining: expected_size,
        digest: Sha256::new(),
        asset_revision: include_asset_revision.then(|| asset_revision_hasher(expected_size)),
    };
    let result = inspect(&mut verified)?;
    if cancelled() {
        return Err("materialization_cancelled");
    }
    if verified.remaining != 0 || format!("{:x}", verified.digest.finalize()) != identity {
        return Err("materialization_conflict");
    }
    let asset_revision = verified
        .asset_revision
        .map(|digest| format!("{:x}", digest.finalize()));
    let revalidated = input
        .metadata()
        .map_err(|_| "materialization_unavailable")?;
    if immutable_file_identity(&revalidated) != initial_identity {
        return Err("materialization_conflict");
    }
    Ok((result, asset_revision, initial_identity))
}

#[cfg(test)]
pub fn read_verified_bytes_cancellable<F>(
    root: &Path,
    identity: &str,
    expected_size: u64,
    maximum_size: usize,
    cancelled: &F,
) -> Result<Vec<u8>, &'static str>
where
    F: Fn() -> bool,
{
    if !valid_identity(identity)
        || expected_size == 0
        || maximum_size == 0
        || expected_size > u64::try_from(maximum_size).map_err(|_| "invalid_materialization")?
    {
        return Err("invalid_materialization");
    }
    let target = path(root, identity);
    let (mut input, initial_identity, _asset_revision) =
        open_verified_cancellable(&target, identity, expected_size, None, cancelled, false)?;
    if cancelled() {
        return Err("materialization_cancelled");
    }
    input
        .seek(SeekFrom::Start(0))
        .map_err(|_| "materialization_unavailable")?;
    let mut output =
        Vec::with_capacity(usize::try_from(expected_size).map_err(|_| "invalid_materialization")?);
    {
        let mut limited = (&mut input).take(
            expected_size
                .checked_add(1)
                .ok_or("invalid_materialization")?,
        );
        let mut buffer = [0u8; 64 * 1024];
        loop {
            if cancelled() {
                return Err("materialization_cancelled");
            }
            let length = limited
                .read(&mut buffer)
                .map_err(|_| "materialization_unavailable")?;
            if length == 0 {
                break;
            }
            output.extend_from_slice(&buffer[..length]);
        }
    }
    if cancelled()
        || output.len() != usize::try_from(expected_size).map_err(|_| "invalid_materialization")?
    {
        return Err(if cancelled() {
            "materialization_cancelled"
        } else {
            "materialization_conflict"
        });
    }
    let revalidated = input
        .metadata()
        .map_err(|_| "materialization_unavailable")?;
    if immutable_file_identity(&revalidated) != initial_identity {
        return Err("materialization_conflict");
    }
    Ok(output)
}

pub fn read_immutable_range_cancellable<F>(
    root: &Path,
    identity: &str,
    expected_size: u64,
    start: u64,
    end: u64,
    expected_identity: ImmutableFileIdentity,
    cancelled: &F,
) -> Result<Vec<u8>, &'static str>
where
    F: Fn() -> bool,
{
    let mut reader = ImmutableRangeReader::open(root, identity, expected_size, expected_identity)?;
    let output = reader.read_range_cancellable(start, end, cancelled)?;
    reader.revalidate()?;
    Ok(output)
}

impl ImmutableRangeReader {
    pub fn open(
        root: &Path,
        identity: &str,
        expected_size: u64,
        expected_identity: ImmutableFileIdentity,
    ) -> Result<Self, &'static str> {
        let input = open_nofollow(&path(root, identity))?;
        let metadata = input
            .metadata()
            .map_err(|_| "materialization_unavailable")?;
        if !sealed_file_metadata(&metadata, expected_size)
            || immutable_file_identity(&metadata) != expected_identity
        {
            return Err("materialization_conflict");
        }
        Ok(Self {
            input,
            expected_size,
            expected_identity,
        })
    }

    pub fn read_range_cancellable<F>(
        &mut self,
        start: u64,
        end: u64,
        cancelled: &F,
    ) -> Result<Vec<u8>, &'static str>
    where
        F: Fn() -> bool,
    {
        const MAX_RANGE_BYTES: u64 = 8 * 1024 * 1024;
        if start > end || end >= self.expected_size {
            return Err("invalid_materialization_range");
        }
        let length = end - start + 1;
        if length > MAX_RANGE_BYTES {
            return Err("materialization_range_too_large");
        }
        if cancelled() {
            return Err("materialization_cancelled");
        }
        self.input
            .seek(SeekFrom::Start(start))
            .map_err(|_| "materialization_unavailable")?;
        let mut output = Vec::with_capacity(length as usize);
        let mut remaining = length;
        let mut buffer = [0_u8; 64 * 1024];
        while remaining != 0 {
            if cancelled() {
                return Err("materialization_cancelled");
            }
            let wanted = remaining.min(buffer.len() as u64) as usize;
            let read = self
                .input
                .read(&mut buffer[..wanted])
                .map_err(|_| "materialization_unavailable")?;
            if read == 0 {
                return Err("materialization_conflict");
            }
            output.extend_from_slice(&buffer[..read]);
            remaining -= read as u64;
        }
        Ok(output)
    }

    pub fn revalidate(&self) -> Result<(), &'static str> {
        let metadata = self
            .input
            .metadata()
            .map_err(|_| "materialization_unavailable")?;
        if !sealed_file_metadata(&metadata, self.expected_size)
            || immutable_file_identity(&metadata) != self.expected_identity
        {
            return Err("materialization_conflict");
        }
        Ok(())
    }
}

#[cfg(test)]
pub fn verified_samples(
    root: &Path,
    identity: &str,
    expected_size: u64,
    sample_bytes: usize,
) -> Result<(Vec<u8>, Vec<u8>, ImmutableFileIdentity), &'static str> {
    verified_samples_cancellable(root, identity, expected_size, sample_bytes, &|| false)
}

pub fn verified_samples_cancellable<F>(
    root: &Path,
    identity: &str,
    expected_size: u64,
    sample_bytes: usize,
    cancelled: &F,
) -> Result<(Vec<u8>, Vec<u8>, ImmutableFileIdentity), &'static str>
where
    F: Fn() -> bool,
{
    verified_samples_trusted_cancellable(
        root,
        identity,
        expected_size,
        sample_bytes,
        None,
        cancelled,
    )
}

pub(crate) fn verified_samples_trusted_cancellable<F>(
    root: &Path,
    identity: &str,
    expected_size: u64,
    sample_bytes: usize,
    trusted_identity: Option<ImmutableFileIdentity>,
    cancelled: &F,
) -> Result<(Vec<u8>, Vec<u8>, ImmutableFileIdentity), &'static str>
where
    F: Fn() -> bool,
{
    if expected_size == 0
        || expected_size > MAX_MATERIALIZATION_BYTES
        || sample_bytes == 0
        || sample_bytes > crate::inspect::MAX_STRUCTURAL_END_BYTES
    {
        return Err("invalid_materialization");
    }
    let target = path(root, identity);
    let (mut input, identity, _asset_revision) = open_verified_cancellable(
        &target,
        identity,
        expected_size,
        trusted_identity,
        cancelled,
        false,
    )?;
    let sample_bytes = sample_bytes as u64;
    let length = expected_size.min(sample_bytes) as usize;
    if cancelled() {
        return Err("materialization_cancelled");
    }
    let mut head = vec![0; length];
    input
        .seek(SeekFrom::Start(0))
        .and_then(|_| input.read_exact(&mut head))
        .map_err(|_| "materialization_unavailable")?;
    if cancelled() {
        return Err("materialization_cancelled");
    }
    let mut tail = vec![0; length];
    input
        .seek(SeekFrom::Start(expected_size - length as u64))
        .and_then(|_| input.read_exact(&mut tail))
        .map_err(|_| "materialization_unavailable")?;
    let revalidated_identity = input
        .metadata()
        .map(|metadata| immutable_file_identity(&metadata))
        .map_err(|_| "materialization_unavailable")?;
    if revalidated_identity != identity {
        return Err("materialization_conflict");
    }
    Ok((head, tail, identity))
}

#[cfg(test)]
mod tests {
    use super::{
        ArchiveExtractionStage, Par2RepairStage, PartialSourceStage, StagedMaterialization,
        VerifiedMaterializationPart, asset_revision_hasher, cleanup_staging,
        inspect_verified_cancellable, publish, publish_generated, publish_generated_auto,
        read_immutable_range_cancellable, read_verified_bytes_cancellable, verified_path,
        verified_samples, verified_samples_cancellable,
    };
    use crate::cache::VerifiedSegment;
    use crate::yenc::DecodedPart;
    use sha2::{Digest, Sha256};
    use std::fs::File;
    use std::os::unix::fs::PermissionsExt;
    use std::sync::atomic::{AtomicU64, Ordering};

    static ROOT_SEQUENCE: AtomicU64 = AtomicU64::new(0);

    fn root() -> std::path::PathBuf {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("system clock")
            .as_nanos();
        let sequence = ROOT_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!(
            "comet-materialization-{}-{nonce}-{sequence}",
            std::process::id()
        ))
    }

    #[test]
    fn asset_revision_has_a_stable_chunking_independent_golden_vector() {
        let mut split = asset_revision_hasher(3);
        split.update(b"A");
        split.update(b"BC");
        let mut whole = asset_revision_hasher(3);
        whole.update(b"ABC");

        let expected = "b37f7ae393301b129dd6f0370c14bd67e4954ab64cd772c724c32952d06c8cee";
        assert_eq!(format!("{:x}", split.finalize()), expected);
        assert_eq!(format!("{:x}", whole.finalize()), expected);
    }

    fn part(bytes: &[u8], begin: u64, end: u64, total_size: u64) -> DecodedPart {
        DecodedPart {
            bytes: bytes.to_vec(),
            begin,
            end,
            total_size,
            expected_crc32: Some(1),
            expected_whole_crc32: None,
        }
    }

    #[test]
    fn publishes_only_contiguous_verified_parts() {
        let root = root();
        let identity = format!("{:x}", Sha256::digest(b"ABC"));
        let target = publish(
            &root,
            &identity,
            vec![part(b"C", 3, 3, 3), part(b"AB", 1, 2, 3)],
            3,
        )
        .unwrap();

        assert_eq!(std::fs::read(target).unwrap(), b"ABC");
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn rejects_gaps_before_publishing_bytes() {
        let root = root();
        let identity = format!("{:x}", Sha256::digest(b"AC"));

        assert_eq!(
            publish(
                &root,
                &identity,
                vec![part(b"A", 1, 1, 2), part(b"C", 3, 3, 2)],
                2
            ),
            Err("materialization_gap")
        );
        assert!(
            !root
                .join("materialized")
                .join(format!("{identity}.bin"))
                .exists()
        );
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn reads_only_a_bounded_verified_range() {
        let root = root();
        let identity = format!("{:x}", Sha256::digest(b"ABCDEF"));
        let target = publish(&root, &identity, vec![part(b"ABCDEF", 1, 6, 6)], 6).unwrap();

        let (head, tail, file_identity) =
            verified_samples(&root, &identity, 6, 2).expect("verified samples");
        assert_eq!((head, tail), (b"AB".to_vec(), b"EF".to_vec()));
        assert_eq!(
            read_immutable_range_cancellable(&root, &identity, 6, 1, 3, file_identity, &|| false),
            Ok(b"BCD".to_vec())
        );
        assert_eq!(
            read_immutable_range_cancellable(&root, &identity, 6, 1, 3, file_identity, &|| true),
            Err("materialization_cancelled")
        );
        assert_eq!(
            verified_samples_cancellable(&root, &identity, 6, 2, &|| true),
            Err("materialization_cancelled")
        );
        assert_eq!(
            read_immutable_range_cancellable(&root, &identity, 6, 4, 2, file_identity, &|| false),
            Err("invalid_materialization_range")
        );
        std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o600)).unwrap();
        std::fs::write(&target, b"ABXDEF").unwrap();
        std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o400)).unwrap();
        assert_eq!(
            read_immutable_range_cancellable(&root, &identity, 6, 1, 3, file_identity, &|| false),
            Err("materialization_conflict")
        );
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn reopens_and_reads_only_a_bounded_immutable_materialization() {
        let root = root();
        let identity = format!("{:x}", Sha256::digest(b"ABCDEF"));
        publish(&root, &identity, vec![part(b"ABCDEF", 1, 6, 6)], 6).unwrap();

        assert_eq!(
            read_verified_bytes_cancellable(&root, &identity, 6, 6, &|| false),
            Ok(b"ABCDEF".to_vec())
        );
        assert_eq!(
            read_verified_bytes_cancellable(&root, &identity, 6, 5, &|| false),
            Err("invalid_materialization")
        );
        assert_eq!(
            read_verified_bytes_cancellable(&root, &identity, 6, 6, &|| true),
            Err("materialization_cancelled")
        );
        assert_eq!(
            inspect_verified_cancellable(&root, &identity, 6, &|| false, |input| {
                let mut bytes = Vec::new();
                std::io::Read::read_to_end(input, &mut bytes).unwrap();
                Ok(bytes)
            }),
            Ok(b"ABCDEF".to_vec())
        );
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn refuses_a_materialization_that_lost_its_immutable_metadata() {
        let root = root();
        let identity = format!("{:x}", Sha256::digest(b"ABC"));
        let target = publish(&root, &identity, vec![part(b"ABC", 1, 3, 3)], 3).unwrap();
        let alias = root.join("materialized-alias.bin");
        std::fs::hard_link(&target, &alias).unwrap();

        assert_eq!(
            read_verified_bytes_cancellable(&root, &identity, 3, 3, &|| false),
            Err("materialization_conflict")
        );
        std::fs::remove_file(alias).unwrap();
        std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o600)).unwrap();

        assert_eq!(
            read_verified_bytes_cancellable(&root, &identity, 3, 3, &|| false),
            Err("materialization_conflict")
        );
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn refuses_a_materialization_whose_contents_no_longer_match_its_identity() {
        let root = root();
        let identity = format!("{:x}", Sha256::digest(b"ABC"));
        let target = publish(&root, &identity, vec![part(b"ABC", 1, 3, 3)], 3).unwrap();
        std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o600)).unwrap();
        std::fs::write(&target, b"ABD").unwrap();
        std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o400)).unwrap();

        assert_eq!(
            read_verified_bytes_cancellable(&root, &identity, 3, 3, &|| false),
            Err("materialization_conflict")
        );
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn derives_identity_only_after_verified_parts_are_complete() {
        let root = root();
        let (target, identity) = publish_generated(&root, vec![part(b"ABC", 1, 3, 3)], 3).unwrap();

        assert_eq!(identity, format!("{:x}", Sha256::digest(b"ABC")));
        assert_eq!(std::fs::read(target).unwrap(), b"ABC");
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn derives_materialized_size_from_verified_parts() {
        let root = root();
        let (_, _, size) =
            publish_generated_auto(&root, vec![part(b"A", 1, 1, 3), part(b"BC", 2, 3, 3)]).unwrap();

        assert_eq!(size, 3);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn rejects_a_truncated_multipart_logical_file() {
        let root = root();

        assert_eq!(
            publish_generated_auto(&root, vec![part(b"A", 1, 1, 2)]),
            Err("materialization_size_mismatch")
        );
        assert!(!root.join("materialized").exists());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn rejects_a_mismatched_whole_file_checksum() {
        let root = root();
        let identity = format!("{:x}", Sha256::digest(b"AB"));
        let mut first = part(b"A", 1, 1, 2);
        first.expected_whole_crc32 = Some(0);

        assert_eq!(
            publish(&root, &identity, vec![first, part(b"B", 2, 2, 2)], 2),
            Err("materialization_checksum_mismatch")
        );
        assert!(
            !root
                .join("materialized")
                .join(format!("{identity}.bin"))
                .exists()
        );
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn rejects_materializations_above_the_logical_limit() {
        let root = root();
        let identity = format!("{:x}", Sha256::digest(b"A"));
        let size = super::MAX_MATERIALIZATION_BYTES + 1;

        assert_eq!(
            publish(&root, &identity, vec![part(b"A", 1, 1, size)], size),
            Err("invalid_materialization")
        );
        assert!(!root.join("materialized").exists());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn stages_parts_without_retaining_a_complete_file_in_memory() {
        let root = root();
        let (target, identity, size, asset_revision) = {
            let mut stage = StagedMaterialization::new(&root).unwrap();
            let tail = VerifiedSegment::from_decoded(part(b"C", 3, 3, 3)).unwrap();
            let head = VerifiedSegment::from_decoded(part(b"AB", 1, 2, 3)).unwrap();
            stage.push_segment(&tail).unwrap();
            stage.push_segment(&head).unwrap();
            assert_eq!(std::fs::read_dir(&stage.directory).unwrap().count(), 1);
            assert_eq!(std::fs::metadata(&stage.path).unwrap().len(), 3);
            stage.publish().unwrap()
        };

        assert_eq!(identity, format!("{:x}", Sha256::digest(b"ABC")));
        assert_eq!(size, 3);
        assert_eq!(
            asset_revision,
            "b37f7ae393301b129dd6f0370c14bd67e4954ab64cd772c724c32952d06c8cee"
        );
        assert_eq!(std::fs::read(target).unwrap(), b"ABC");
        assert_eq!(std::fs::read_dir(root.join("staging")).unwrap().count(), 0);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn publishes_complete_bytes_outside_the_replica_local_stage() {
        let local = root();
        let shared = root();
        let (target, identity, size, _) = {
            let mut stage = StagedMaterialization::new_with_publication(&local, &shared).unwrap();
            let segment = VerifiedSegment::from_decoded(part(b"ABC", 1, 3, 3)).unwrap();
            stage.push_segment(&segment).unwrap();
            stage.publish().unwrap()
        };

        assert_eq!(target.parent(), Some(shared.join("materialized").as_path()));
        assert_eq!(std::fs::read(&target).unwrap(), b"ABC");
        assert_eq!(verified_path(&shared, &identity, size).unwrap(), target);
        assert!(!local.join("materialized").exists());
        assert_eq!(std::fs::read_dir(local.join("staging")).unwrap().count(), 0);
        let _ = std::fs::remove_dir_all(local);
        let _ = std::fs::remove_dir_all(shared);
    }

    #[test]
    fn independently_publishes_a_verified_archive_worker_output() {
        let root = root();
        let (target, identity, size, asset_revision) = {
            let stage = ArchiveExtractionStage::new(&root).unwrap();
            std::fs::write(stage.output(), b"archive member").unwrap();
            std::fs::set_permissions(stage.output(), std::fs::Permissions::from_mode(0o400))
                .unwrap();
            stage.publish().unwrap()
        };

        assert_eq!(identity, format!("{:x}", Sha256::digest(b"archive member")));
        assert_eq!(size, 14);
        assert_eq!(
            asset_revision,
            "9c8f57be2011670af22ec9457a36524f0c249a35fbd1df7c959db8b8258b10ec"
        );
        assert_eq!(std::fs::read(&target).unwrap(), b"archive member");
        assert_eq!(verified_path(&root, &identity, size).unwrap(), target);
        assert_eq!(
            std::fs::metadata(&target).unwrap().permissions().mode() & 0o777,
            0o400
        );
        assert_eq!(std::fs::read_dir(root.join("staging")).unwrap().count(), 0);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn publishes_repaired_output_from_a_configured_symlink_data_root() {
        let root = root();
        std::fs::create_dir(&root).unwrap();
        let alias = root.with_extension("alias");
        std::os::unix::fs::symlink(&root, &alias).unwrap();
        let mut stage = Par2RepairStage::new(&alias).unwrap();
        stage.create_source("Movie.mkv", 14).unwrap();
        std::fs::write(stage.directory.join("Movie.mkv"), b"repaired movie").unwrap();

        let (target, _, _, _, ()) = stage
            .publish_verified_selected("Movie.mkv", 14, |_| Ok(()))
            .unwrap();

        assert_eq!(std::fs::read(target).unwrap(), b"repaired movie");
        std::fs::remove_file(alias).unwrap();
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn stages_sparse_par2_sources_and_publishes_only_independently_verified_output() {
        let root = root();
        let known_identity = format!("{:x}", Sha256::digest(b"KNOWN"));
        publish(&root, &known_identity, vec![part(b"KNOWN", 1, 5, 5)], 5).unwrap();

        let (target, identity, size, asset_revision, inspected) = {
            let mut stage = Par2RepairStage::new(&root).unwrap();
            stage.create_source("nested/Known.bin", 5).unwrap();
            stage
                .copy_complete_source(&known_identity, 5, "nested/Known.bin", &|| false)
                .unwrap();
            stage.create_source("Movie.mkv", 4).unwrap();
            std::fs::write(stage.directory.join("Movie.mkv"), b"DATA").unwrap();
            let index_input = root.join("repair-input.par2");
            std::fs::write(&index_input, b"PAR2").unwrap();
            stage
                .write_index_ranges(
                    &mut [File::open(index_input).unwrap()],
                    &[vec![(0, 4)]],
                    &|| false,
                )
                .unwrap();
            assert_eq!(stage.logical_bytes(), 13);
            assert_eq!(
                std::fs::read(stage.directory.join("nested/Known.bin")).unwrap(),
                b"KNOWN"
            );
            stage
                .publish_verified_selected("Movie.mkv", 4, |input| {
                    let mut bytes = Vec::new();
                    std::io::Read::read_to_end(input, &mut bytes)
                        .map_err(|_| "test_read_failed")?;
                    (bytes == b"DATA")
                        .then_some("verified")
                        .ok_or("par2_file_evidence_invalid")
                })
                .unwrap()
        };

        assert_eq!(identity, format!("{:x}", Sha256::digest(b"DATA")));
        assert_eq!(size, 4);
        assert_eq!(
            asset_revision,
            "072572212b4431ff3bab27ac2c0d36c9562be33cc100979d6e8bf11cd5a96753"
        );
        assert_eq!(inspected, "verified");
        assert_eq!(std::fs::read(&target).unwrap(), b"DATA");
        assert_eq!(
            std::fs::metadata(&target).unwrap().permissions().mode() & 0o777,
            0o400
        );
        assert_eq!(std::fs::read_dir(root.join("staging")).unwrap().count(), 0);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn par2_stage_rejects_reserved_paths_and_cancellation() {
        let root = root();
        let identity = format!("{:x}", Sha256::digest(b"A"));
        publish(&root, &identity, vec![part(b"A", 1, 1, 1)], 1).unwrap();
        let mut stage = Par2RepairStage::new(&root).unwrap();

        assert_eq!(
            stage.create_source("INDEX.PAR2", 1),
            Err("repair_stage_path_conflict")
        );
        stage.create_source("source.bin", 1).unwrap();
        assert_eq!(
            stage.copy_complete_source(&identity, 1, "source.bin", &|| true),
            Err("materialization_cancelled")
        );
        let index_input = root.join("repair-input.par2");
        std::fs::write(&index_input, b"PAR2").unwrap();
        assert_eq!(
            stage.write_index_ranges(
                &mut [File::open(index_input).unwrap()],
                &[vec![(0, 4)]],
                &|| true,
            ),
            Err("materialization_cancelled")
        );
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn stages_and_copies_typed_partial_source_slices_without_revalidation() {
        let root = root();
        let mut trailing = PartialSourceStage::new(&root, 7).unwrap();
        trailing.push(part(b"EFG", 5, 7, 7)).unwrap();
        let trailing_evidence = trailing.evidence(4, &|| false).unwrap();
        assert_eq!(
            trailing_evidence.checksums[1].unwrap().md5,
            [
                0xbd, 0xdc, 0xfb, 0xf2, 0xeb, 0x70, 0x71, 0x90, 0x19, 0x26, 0x4d, 0xda, 0x48, 0x97,
                0x60, 0x62,
            ]
        );
        assert_eq!(trailing_evidence.checksums[1].unwrap().crc32, 0x46a1_5fa3);
        drop(trailing);

        let mut partial = PartialSourceStage::new(&root, 8).unwrap();
        partial.push(part(b"AA", 1, 2, 8)).unwrap();
        partial.push(part(b"AA", 3, 4, 8)).unwrap();
        let evidence = partial.evidence(4, &|| false).unwrap();

        assert!(evidence.checksums[0].is_some());
        assert!(evidence.checksums[1].is_none());
        assert!(evidence.full_md5.is_none());
        assert_eq!(
            partial.push(part(b"AB", 4, 5, 8)),
            Err("par2_source_evidence_invalid")
        );

        {
            let mut repair = Par2RepairStage::new(&root).unwrap();
            repair.create_source("source.bin", 8).unwrap();
            repair
                .copy_partial_source(&mut partial, "source.bin", &[true, false], 4, &|| false)
                .unwrap();
            assert_eq!(
                std::fs::read(repair.directory.join("source.bin")).unwrap(),
                b"AAAA\0\0\0\0"
            );
        }
        drop(partial);
        assert_eq!(std::fs::read_dir(root.join("staging")).unwrap().count(), 0);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn adopts_a_sparse_partial_source_without_copying_it() {
        let root = root();
        let mut partial = PartialSourceStage::new(&root, 8).unwrap();
        partial.push(part(b"DATA", 1, 4, 8)).unwrap();
        let mut repair = Par2RepairStage::new(&root).unwrap();

        repair.adopt_partial_source(&partial, "source.bin").unwrap();
        assert_eq!(repair.logical_bytes(), 8);
        assert_eq!(
            std::fs::read(repair.directory.join("source.bin")).unwrap(),
            b"DATA\0\0\0\0"
        );
        drop(partial);
        assert!(repair.directory.join("source.bin").exists());

        drop(repair);
        assert_eq!(std::fs::read_dir(root.join("staging")).unwrap().count(), 0);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn concatenates_only_fingerprint_pinned_archive_volumes() {
        let root = root();
        let first_identity = format!("{:x}", Sha256::digest(b"ABC"));
        let second_identity = format!("{:x}", Sha256::digest(b"DE"));
        publish(&root, &first_identity, vec![part(b"ABC", 1, 3, 3)], 3).unwrap();
        publish(&root, &second_identity, vec![part(b"DE", 1, 2, 2)], 2).unwrap();
        let first_file = verified_samples(&root, &first_identity, 3, 2).unwrap().2;
        let second_file = verified_samples(&root, &second_identity, 2, 2).unwrap().2;
        let parts = [
            VerifiedMaterializationPart {
                content_identity: &first_identity,
                exact_size: 3,
                file_identity: first_file,
            },
            VerifiedMaterializationPart {
                content_identity: &second_identity,
                exact_size: 2,
                file_identity: second_file,
            },
        ];
        {
            let stage = ArchiveExtractionStage::new(&root).unwrap();
            let input = stage.concatenate_verified(&parts, 5, &|| false).unwrap();
            assert_eq!(std::fs::read(input).unwrap(), b"ABCDE");
            assert_eq!(
                std::fs::metadata(input).unwrap().permissions().mode() & 0o777,
                0o400
            );
        }
        {
            let stage = ArchiveExtractionStage::new(&root).unwrap();
            let input = stage
                .concatenate_verified(&parts[..1], 3, &|| false)
                .unwrap();
            assert_eq!(std::fs::read(input).unwrap(), b"ABC");
        }
        {
            let stage = ArchiveExtractionStage::new(&root).unwrap();
            assert_eq!(
                stage.concatenate_verified(&parts, 5, &|| true),
                Err("materialization_cancelled")
            );
        }
        assert_eq!(std::fs::read_dir(root.join("staging")).unwrap().count(), 0);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn rejects_a_writable_archive_worker_output() {
        let root = root();
        let stage = ArchiveExtractionStage::new(&root).unwrap();
        std::fs::write(stage.output(), b"untrusted").unwrap();

        assert_eq!(stage.publish(), Err("materialization_conflict"));
        assert!(!root.join("materialized").exists());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn removes_only_abandoned_private_stage_directories() {
        let root = root();
        let staging = root.join("staging");
        std::fs::create_dir_all(staging.join(".stage-crashed")).unwrap();
        std::fs::write(staging.join(".stage-crashed").join("part-0"), b"orphan").unwrap();
        std::fs::create_dir_all(staging.join(".archive-crashed")).unwrap();
        std::fs::write(
            staging.join(".archive-crashed").join("selected.bin"),
            b"orphan",
        )
        .unwrap();
        std::fs::create_dir_all(staging.join("keep")).unwrap();

        cleanup_staging(&root).unwrap();

        assert!(!staging.join(".stage-crashed").exists());
        assert!(!staging.join(".archive-crashed").exists());
        assert!(staging.join("keep").exists());
        let _ = std::fs::remove_dir_all(root);
    }
}
