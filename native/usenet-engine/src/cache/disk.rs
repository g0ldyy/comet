use super::confined::ConfinedDirectory;
use super::{Admission, SegmentCacheKey, VerifiedSegment};
use crate::yenc::DecodedPart;
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

const MAX_VERIFIED_SEGMENT_BYTES: usize = crate::limits::MAX_DECLARED_POSTING_BYTES as usize;
const BLOB_VERIFICATION_BUFFER_BYTES: usize = 64 * 1024;
const EVICTION_BATCH_SIZE: i64 = 256;
const LRU_TOUCH_INTERVAL_MS: i64 = 30_000;
// The STRICT table row plus its primary-key, digest and LRU indexes consume
// persistent SQLite pages even when many keys share one physical blob.
const DISK_MAPPING_OVERHEAD_BYTES: u64 = 512;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct DiskCacheStats {
    pub mappings: usize,
    pub blobs: usize,
    pub used_bytes: u64,
}

struct Mapping {
    digest: [u8; 32],
    byte_size: u64,
    begin: u64,
    end: u64,
    total_size: u64,
    part_crc32: u32,
    whole_crc32: Option<u32>,
    last_access_ms: i64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum BlobReadError {
    Invalid,
    Unavailable,
}

impl BlobReadError {
    fn code(self) -> &'static str {
        match self {
            Self::Invalid => "disk_cache_corrupt",
            Self::Unavailable => "disk_cache_unavailable",
        }
    }
}

pub struct DiskSegmentCache {
    blobs_directory: ConfinedDirectory,
    budget: u64,
    minimum_free_bytes: u64,
    connection: Connection,
}

impl DiskSegmentCache {
    pub fn open(
        local_data_root: &Path,
        budget: u64,
        minimum_free_bytes: u64,
    ) -> Result<Self, &'static str> {
        if budget == 0 {
            return Err("disk_cache_disabled");
        }
        let root = local_data_root.join("cache").join("segments");
        let blobs = root.join("blobs");
        secure_directory(&root)?;
        secure_directory(&blobs)?;
        let blobs_directory =
            ConfinedDirectory::open(&blobs).map_err(|_| "disk_cache_unavailable")?;
        remove_abandoned_temporaries(&blobs_directory)?;
        let index = root.join("index.sqlite3");
        secure_regular_file(&index)?;
        let connection = Connection::open(index).map_err(|_| "disk_cache_unavailable")?;
        connection
            .pragma_update(None, "journal_mode", "WAL")
            .map_err(|_| "disk_cache_unavailable")?;
        connection
            .pragma_update(None, "synchronous", "NORMAL")
            .map_err(|_| "disk_cache_unavailable")?;
        connection
            .pragma_update(None, "foreign_keys", "ON")
            .map_err(|_| "disk_cache_unavailable")?;
        connection
            .execute_batch(
                "
                CREATE TABLE IF NOT EXISTS blobs (
                    digest BLOB PRIMARY KEY CHECK (length(digest) = 32),
                    byte_size INTEGER NOT NULL CHECK (byte_size > 0),
                    allocated_bytes INTEGER NOT NULL CHECK (allocated_bytes > 0),
                    last_access_ms INTEGER NOT NULL CHECK (last_access_ms >= 0)
                ) STRICT;
                CREATE TABLE IF NOT EXISTS acquisition_mappings (
                    acquisition_key BLOB PRIMARY KEY CHECK (length(acquisition_key) = 32),
                    digest BLOB NOT NULL REFERENCES blobs(digest) ON DELETE CASCADE,
                    begin_offset INTEGER NOT NULL CHECK (begin_offset > 0),
                    end_offset INTEGER NOT NULL CHECK (end_offset >= begin_offset),
                    total_size INTEGER NOT NULL CHECK (total_size >= end_offset),
                    part_crc32 INTEGER NOT NULL CHECK (part_crc32 BETWEEN 0 AND 4294967295),
                    whole_crc32 INTEGER CHECK (whole_crc32 BETWEEN 0 AND 4294967295),
                    last_access_ms INTEGER NOT NULL CHECK (last_access_ms >= 0)
                ) STRICT;
                CREATE INDEX IF NOT EXISTS idx_acquisition_mappings_lru_v1
                    ON acquisition_mappings(last_access_ms, acquisition_key);
                CREATE INDEX IF NOT EXISTS idx_acquisition_mappings_digest_v1
                    ON acquisition_mappings(digest);
                ",
            )
            .map_err(|_| "disk_cache_unavailable")?;
        let mut cache = Self {
            blobs_directory,
            budget,
            minimum_free_bytes,
            connection,
        };
        cache.reconcile()?;
        Ok(cache)
    }

    pub fn budget(&self) -> u64 {
        self.budget
    }

    pub fn get(&mut self, key: SegmentCacheKey) -> Result<Option<VerifiedSegment>, &'static str> {
        let mapping = self
            .connection
            .query_row(
                "
                SELECT digest, b.byte_size, begin_offset, end_offset, total_size,
                       part_crc32, whole_crc32, m.last_access_ms
                FROM acquisition_mappings m
                JOIN blobs b USING (digest)
                WHERE acquisition_key = ?1
                ",
                params![key.0.as_slice()],
                mapping_from_row,
            )
            .optional()
            .map_err(|_| "disk_cache_unavailable")?;
        let Some(mapping) = mapping else {
            return Ok(None);
        };
        let name = blob_name(&mapping.digest);
        let bytes = match read_verified_blob(
            &self.blobs_directory,
            &name,
            &mapping.digest,
            mapping.byte_size,
        ) {
            Ok(bytes) => bytes,
            Err(BlobReadError::Invalid) => {
                self.invalidate_blob(mapping.digest)?;
                return Err(BlobReadError::Invalid.code());
            }
            Err(BlobReadError::Unavailable) => return Err("disk_cache_unavailable"),
        };
        let now = unix_milliseconds()?;
        if now.saturating_sub(mapping.last_access_ms) >= LRU_TOUCH_INTERVAL_MS {
            let transaction = self
                .connection
                .transaction_with_behavior(TransactionBehavior::Immediate)
                .map_err(|_| "disk_cache_unavailable")?;
            transaction
                .execute(
                    "UPDATE acquisition_mappings SET last_access_ms = ?2 WHERE acquisition_key = ?1",
                    params![key.0.as_slice(), now],
                )
                .map_err(|_| "disk_cache_unavailable")?;
            transaction
                .execute(
                    "UPDATE blobs SET last_access_ms = ?2 WHERE digest = ?1",
                    params![mapping.digest.as_slice(), now],
                )
                .map_err(|_| "disk_cache_unavailable")?;
            transaction.commit().map_err(|_| "disk_cache_unavailable")?;
        }
        VerifiedSegment::from_decoded(DecodedPart {
            bytes,
            begin: mapping.begin,
            end: mapping.end,
            total_size: mapping.total_size,
            expected_crc32: Some(mapping.part_crc32),
            expected_whole_crc32: mapping.whole_crc32,
        })
        .map(Some)
    }

    pub fn insert(
        &mut self,
        key: SegmentCacheKey,
        segment: &VerifiedSegment,
    ) -> Result<Admission, &'static str> {
        let bytes = segment.bytes.as_ref();
        let byte_size = bytes.len() as u64;
        if byte_size > self.budget / 8 {
            return Ok(Admission::Bypassed);
        }
        let digest: [u8; 32] = Sha256::digest(bytes).into();
        let target = blob_name(&digest);
        let blob_exists = self
            .blobs_directory
            .exists(&target)
            .map_err(|_| "disk_cache_unavailable")?;
        let replaced = self
            .connection
            .query_row(
                "SELECT EXISTS(
                    SELECT 1 FROM acquisition_mappings WHERE acquisition_key = ?1
                )",
                params![key.0.as_slice()],
                |row| row.get(0),
            )
            .map_err(|_| "disk_cache_unavailable")?;
        let additional_blob_bytes = if blob_exists { 0 } else { byte_size };
        let additional_bytes = additional_blob_bytes
            + if replaced {
                0
            } else {
                DISK_MAPPING_OVERHEAD_BYTES
            };
        let available = self
            .blobs_directory
            .available_bytes()
            .map_err(|_| "disk_cache_unavailable")?;
        if available < additional_bytes || available - additional_bytes < self.minimum_free_bytes {
            return Ok(Admission::Bypassed);
        }
        let (allocated_bytes, created) =
            self.publish_blob(&target, digest, byte_size, bytes, blob_exists)?;
        let now = unix_milliseconds()?;
        let evicted =
            match self.commit_mapping(key, segment, digest, byte_size, allocated_bytes, now) {
                Ok(evicted) => evicted,
                Err(error) => {
                    if created {
                        self.remove_blob(digest)?;
                    }
                    return Err(error);
                }
            };
        for evicted_digest in evicted {
            self.remove_blob(evicted_digest)?;
        }
        Ok(if replaced {
            Admission::Replaced
        } else {
            Admission::Admitted
        })
    }

    pub fn stats(&self) -> Result<DiskCacheStats, &'static str> {
        self.connection
            .query_row(
                "
                SELECT
                    (SELECT count(*) FROM acquisition_mappings),
                    count(*),
                    COALESCE(sum(allocated_bytes), 0)
                        + (SELECT count(*) FROM acquisition_mappings) * ?1
                FROM blobs
                ",
                params![to_i64(DISK_MAPPING_OVERHEAD_BYTES)?],
                |row| {
                    let mappings = usize::try_from(row.get::<_, i64>(0)?)
                        .map_err(|error| integer_conversion_error(0, error))?;
                    let blobs = usize::try_from(row.get::<_, i64>(1)?)
                        .map_err(|error| integer_conversion_error(1, error))?;
                    let used_bytes = u64::try_from(row.get::<_, i64>(2)?)
                        .map_err(|error| integer_conversion_error(2, error))?;
                    Ok(DiskCacheStats {
                        mappings,
                        blobs,
                        used_bytes,
                    })
                },
            )
            .map_err(|_| "disk_cache_unavailable")
    }

    fn commit_mapping(
        &mut self,
        key: SegmentCacheKey,
        segment: &VerifiedSegment,
        digest: [u8; 32],
        byte_size: u64,
        allocated_bytes: u64,
        now: i64,
    ) -> Result<Vec<[u8; 32]>, &'static str> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| "disk_cache_unavailable")?;
        transaction
            .execute(
                "
                INSERT INTO blobs(digest, byte_size, allocated_bytes, last_access_ms)
                VALUES (?1, ?2, ?3, ?4)
                ON CONFLICT(digest) DO UPDATE SET last_access_ms = excluded.last_access_ms
                ",
                params![
                    digest.as_slice(),
                    to_i64(byte_size)?,
                    to_i64(allocated_bytes)?,
                    now
                ],
            )
            .map_err(|_| "disk_cache_unavailable")?;
        transaction
            .execute(
                "
                INSERT INTO acquisition_mappings(
                    acquisition_key, digest, begin_offset, end_offset, total_size,
                    part_crc32, whole_crc32, last_access_ms
                ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)
                ON CONFLICT(acquisition_key) DO UPDATE SET
                    digest = excluded.digest,
                    begin_offset = excluded.begin_offset,
                    end_offset = excluded.end_offset,
                    total_size = excluded.total_size,
                    part_crc32 = excluded.part_crc32,
                    whole_crc32 = excluded.whole_crc32,
                    last_access_ms = excluded.last_access_ms
                ",
                params![
                    key.0.as_slice(),
                    digest.as_slice(),
                    to_i64(segment.begin)?,
                    to_i64(segment.end)?,
                    to_i64(segment.total_size)?,
                    i64::from(segment.part_crc32),
                    segment.whole_crc32.map(i64::from),
                    now
                ],
            )
            .map_err(|_| "disk_cache_unavailable")?;
        let mut evicted = remove_unreferenced_blobs(&transaction)?;
        let mut used = used_bytes(&transaction)?;
        if used > self.budget {
            let target = self.budget / 10 * 9 + self.budget % 10 * 9 / 10;
            while used > target {
                let candidates = transaction
                    .prepare(
                        "
                        SELECT m.acquisition_key, m.digest, b.allocated_bytes,
                               (
                                   SELECT count(*)
                                   FROM acquisition_mappings r
                                   WHERE r.digest = m.digest
                               )
                        FROM acquisition_mappings m
                        JOIN blobs b USING (digest)
                        WHERE m.acquisition_key != ?1
                        ORDER BY m.last_access_ms, m.acquisition_key
                        LIMIT ?2
                        ",
                    )
                    .and_then(|mut statement| {
                        statement
                            .query_map(params![key.0.as_slice(), EVICTION_BATCH_SIZE], |row| {
                                Ok((
                                    fixed_digest(row.get_ref(0)?.as_blob()?)?,
                                    fixed_digest(row.get_ref(1)?.as_blob()?)?,
                                    to_u64_sql(row.get(2)?)?,
                                    to_u64_sql(row.get(3)?)?,
                                ))
                            })?
                            .collect::<Result<Vec<_>, _>>()
                    })
                    .map_err(|_| "disk_cache_unavailable")?;
                if candidates.is_empty() {
                    break;
                }
                let mut projected = used;
                let mut removed_references = HashMap::with_capacity(candidates.len());
                let mut victim_count = 0;
                for (_, digest, allocated_bytes, references) in &candidates {
                    projected -= DISK_MAPPING_OVERHEAD_BYTES;
                    let removed = removed_references.entry(*digest).or_insert(0);
                    *removed += 1;
                    if *removed == *references {
                        projected -= allocated_bytes;
                    }
                    victim_count += 1;
                    if projected <= target {
                        break;
                    }
                }
                for (victim, _, _, _) in candidates.iter().take(victim_count) {
                    transaction
                        .execute(
                            "DELETE FROM acquisition_mappings WHERE acquisition_key = ?1",
                            params![victim.as_slice()],
                        )
                        .map_err(|_| "disk_cache_unavailable")?;
                }
                evicted.extend(remove_unreferenced_blobs(&transaction)?);
                used = used_bytes(&transaction)?;
            }
            if used > self.budget {
                return Err("disk_cache_capacity_unavailable");
            }
        }
        transaction.commit().map_err(|_| "disk_cache_unavailable")?;
        Ok(evicted)
    }

    fn publish_blob(
        &self,
        target: &str,
        digest: [u8; 32],
        byte_size: u64,
        bytes: &[u8],
        exists: bool,
    ) -> Result<(u64, bool), &'static str> {
        if exists {
            return verify_blob(&self.blobs_directory, target, &digest, byte_size)
                .map(|size| (size, false))
                .map_err(BlobReadError::code);
        }
        let temporary = format!(".segment-{}-{}.tmp", std::process::id(), nonce()?);
        let mut output = self
            .blobs_directory
            .create_new(&temporary, 0o600)
            .map_err(|_| "disk_cache_unavailable")?;
        let result = (|| {
            output
                .write_all(bytes)
                .map_err(|_| "disk_cache_unavailable")?;
            output
                .set_permissions(fs::Permissions::from_mode(0o400))
                .map_err(|_| "disk_cache_unavailable")?;
            let published = match self
                .blobs_directory
                .hard_link_no_replace(&temporary, target)
            {
                Ok(()) => (
                    allocated_size(&output.metadata().map_err(|_| "disk_cache_unavailable")?)?,
                    true,
                ),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => (
                    verify_blob(&self.blobs_directory, target, &digest, byte_size)
                        .map_err(BlobReadError::code)?,
                    false,
                ),
                Err(_) => return Err("disk_cache_unavailable"),
            };
            Ok(published)
        })();
        self.blobs_directory
            .remove(&temporary)
            .map_err(|_| "disk_cache_unavailable")?;
        result
    }

    fn remove_blob(&self, digest: [u8; 32]) -> Result<(), &'static str> {
        match self.blobs_directory.remove(&blob_name(&digest)) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(_) => Err("disk_cache_unavailable"),
        }
    }

    fn invalidate_blob(&mut self, digest: [u8; 32]) -> Result<(), &'static str> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| "disk_cache_unavailable")?;
        transaction
            .execute(
                "DELETE FROM acquisition_mappings WHERE digest = ?1",
                params![digest.as_slice()],
            )
            .map_err(|_| "disk_cache_unavailable")?;
        transaction
            .execute(
                "DELETE FROM blobs WHERE digest = ?1",
                params![digest.as_slice()],
            )
            .map_err(|_| "disk_cache_unavailable")?;
        transaction.commit().map_err(|_| "disk_cache_unavailable")?;
        self.remove_blob(digest)?;
        Ok(())
    }

    fn reconcile(&mut self) -> Result<(), &'static str> {
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| "disk_cache_unavailable")?;
        let unreferenced = remove_unreferenced_blobs(&transaction)?;
        transaction.commit().map_err(|_| "disk_cache_unavailable")?;
        for digest in unreferenced {
            self.remove_blob(digest)?;
        }
        let invalid_blobs = {
            let mut statement = self
                .connection
                .prepare("SELECT digest, byte_size FROM blobs")
                .map_err(|_| "disk_cache_unavailable")?;
            let mut invalid = Vec::new();
            let blobs = statement
                .query_map([], |row| {
                    Ok((
                        fixed_digest(row.get_ref(0)?.as_blob()?)?,
                        to_u64_sql(row.get(1)?)?,
                    ))
                })
                .map_err(|_| "disk_cache_unavailable")?;
            for blob in blobs {
                let (digest, byte_size) = blob.map_err(|_| "disk_cache_unavailable")?;
                match inspect_blob(&self.blobs_directory, &blob_name(&digest), byte_size) {
                    Ok(_) => {}
                    Err(BlobReadError::Invalid) => invalid.push(digest),
                    Err(BlobReadError::Unavailable) => return Err("disk_cache_unavailable"),
                }
            }
            invalid
        };
        for digest in invalid_blobs {
            self.invalidate_blob(digest)?;
        }
        let mut indexed_blob = self
            .connection
            .prepare("SELECT EXISTS(SELECT 1 FROM blobs WHERE digest = ?1)")
            .map_err(|_| "disk_cache_unavailable")?;
        for name in self
            .blobs_directory
            .entry_names()
            .map_err(|_| "disk_cache_unavailable")?
        {
            let Some(name) = name.to_str() else {
                continue;
            };
            let Some(digest) = parse_blob_name(name) else {
                continue;
            };
            let indexed: bool = indexed_blob
                .query_row(params![digest.as_slice()], |row| row.get(0))
                .map_err(|_| "disk_cache_unavailable")?;
            if !indexed {
                self.blobs_directory
                    .remove(name)
                    .map_err(|_| "disk_cache_unavailable")?;
            }
        }
        self.blobs_directory
            .sync()
            .map_err(|_| "disk_cache_unavailable")
    }

    #[cfg(test)]
    fn blob_path(&self, digest: &[u8; 32]) -> std::path::PathBuf {
        self.blobs_directory.path().join(blob_name(digest))
    }
}

fn mapping_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Mapping> {
    Ok(Mapping {
        digest: fixed_digest(row.get_ref(0)?.as_blob()?)?,
        byte_size: to_u64_sql(row.get(1)?)?,
        begin: to_u64_sql(row.get(2)?)?,
        end: to_u64_sql(row.get(3)?)?,
        total_size: to_u64_sql(row.get(4)?)?,
        part_crc32: u32::try_from(row.get::<_, i64>(5)?)
            .map_err(|error| integer_conversion_error(5, error))?,
        whole_crc32: row
            .get::<_, Option<i64>>(6)?
            .map(u32::try_from)
            .transpose()
            .map_err(|error| integer_conversion_error(6, error))?,
        last_access_ms: row.get(7)?,
    })
}

fn fixed_digest(value: &[u8]) -> rusqlite::Result<[u8; 32]> {
    value.try_into().map_err(|_| {
        rusqlite::Error::FromSqlConversionFailure(
            0,
            rusqlite::types::Type::Blob,
            "invalid fixed digest".into(),
        )
    })
}

fn to_u64_sql(value: i64) -> rusqlite::Result<u64> {
    u64::try_from(value).map_err(|error| integer_conversion_error(0, error))
}

fn integer_conversion_error(column: usize, error: std::num::TryFromIntError) -> rusqlite::Error {
    rusqlite::Error::FromSqlConversionFailure(
        column,
        rusqlite::types::Type::Integer,
        Box::new(error),
    )
}

fn to_i64(value: u64) -> Result<i64, &'static str> {
    i64::try_from(value).map_err(|_| "disk_cache_unavailable")
}

fn used_bytes(connection: &Connection) -> Result<u64, &'static str> {
    connection
        .query_row(
            "
            SELECT COALESCE(sum(allocated_bytes), 0)
                + (SELECT count(*) FROM acquisition_mappings) * ?1
            FROM blobs
            ",
            params![to_i64(DISK_MAPPING_OVERHEAD_BYTES)?],
            |row| to_u64_sql(row.get(0)?),
        )
        .map_err(|_| "disk_cache_unavailable")
}

fn remove_unreferenced_blobs(connection: &Connection) -> Result<Vec<[u8; 32]>, &'static str> {
    let mut statement = connection
        .prepare(
            "
            DELETE FROM blobs
            WHERE NOT EXISTS (
                SELECT 1 FROM acquisition_mappings m WHERE m.digest = blobs.digest
            )
            RETURNING digest
            ",
        )
        .map_err(|_| "disk_cache_unavailable")?;
    statement
        .query_map([], |row| fixed_digest(row.get_ref(0)?.as_blob()?))
        .map_err(|_| "disk_cache_unavailable")?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| "disk_cache_unavailable")
}

fn read_verified_blob(
    directory: &ConfinedDirectory,
    name: &str,
    expected_digest: &[u8; 32],
    expected_size: u64,
) -> Result<Vec<u8>, BlobReadError> {
    let (mut input, _) = open_blob(directory, name, expected_size)?;
    let mut bytes = vec![0; expected_size as usize];
    read_blob_exact(&mut input, &mut bytes)?;
    finish_blob_read(&mut input, Sha256::digest(&bytes).into(), expected_digest)?;
    Ok(bytes)
}

fn verify_blob(
    directory: &ConfinedDirectory,
    name: &str,
    expected_digest: &[u8; 32],
    expected_size: u64,
) -> Result<u64, BlobReadError> {
    let (mut input, metadata) = open_blob(directory, name, expected_size)?;
    let mut hasher = Sha256::new();
    let mut buffer = [0; BLOB_VERIFICATION_BUFFER_BYTES];
    let mut remaining = expected_size as usize;
    while remaining != 0 {
        let length = remaining.min(buffer.len());
        read_blob_exact(&mut input, &mut buffer[..length])?;
        hasher.update(&buffer[..length]);
        remaining -= length;
    }
    finish_blob_read(&mut input, hasher.finalize().into(), expected_digest)?;
    allocated_size(&metadata).map_err(|_| BlobReadError::Unavailable)
}

fn inspect_blob(
    directory: &ConfinedDirectory,
    name: &str,
    expected_size: u64,
) -> Result<u64, BlobReadError> {
    let (_input, metadata) = open_blob(directory, name, expected_size)?;
    allocated_size(&metadata).map_err(|_| BlobReadError::Unavailable)
}

fn open_blob(
    directory: &ConfinedDirectory,
    name: &str,
    expected_size: u64,
) -> Result<(File, fs::Metadata), BlobReadError> {
    let input = directory.open_read(name).map_err(classify_open_error)?;
    let metadata = input.metadata().map_err(|_| BlobReadError::Unavailable)?;
    if !metadata.file_type().is_file()
        || metadata.len() != expected_size
        || metadata.permissions().mode() & 0o222 != 0
        || expected_size > MAX_VERIFIED_SEGMENT_BYTES as u64
    {
        return Err(BlobReadError::Invalid);
    }
    Ok((input, metadata))
}

fn read_blob_exact(input: &mut File, bytes: &mut [u8]) -> Result<(), BlobReadError> {
    input.read_exact(bytes).map_err(|error| {
        if error.kind() == std::io::ErrorKind::UnexpectedEof {
            BlobReadError::Invalid
        } else {
            BlobReadError::Unavailable
        }
    })
}

fn finish_blob_read(
    input: &mut File,
    digest: [u8; 32],
    expected_digest: &[u8; 32],
) -> Result<(), BlobReadError> {
    let mut trailing = [0u8; 1];
    if input
        .read(&mut trailing)
        .map_err(|_| BlobReadError::Unavailable)?
        != 0
        || digest != *expected_digest
    {
        return Err(BlobReadError::Invalid);
    }
    Ok(())
}

fn classify_open_error(error: std::io::Error) -> BlobReadError {
    if error.kind() == std::io::ErrorKind::NotFound || error.raw_os_error() == Some(libc::ELOOP) {
        BlobReadError::Invalid
    } else {
        BlobReadError::Unavailable
    }
}

fn secure_directory(path: &Path) -> Result<(), &'static str> {
    fs::create_dir_all(path).map_err(|_| "disk_cache_unavailable")?;
    let metadata = fs::symlink_metadata(path).map_err(|_| "disk_cache_unavailable")?;
    if !metadata.file_type().is_dir() {
        return Err("disk_cache_unavailable");
    }
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
        .map_err(|_| "disk_cache_unavailable")
}

fn secure_regular_file(path: &Path) -> Result<(), &'static str> {
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)
        .map_err(|_| "disk_cache_unavailable")?;
    let metadata = file.metadata().map_err(|_| "disk_cache_unavailable")?;
    if !metadata.file_type().is_file() {
        return Err("disk_cache_unavailable");
    }
    file.set_permissions(fs::Permissions::from_mode(0o600))
        .map_err(|_| "disk_cache_unavailable")
}

fn remove_abandoned_temporaries(blobs: &ConfinedDirectory) -> Result<(), &'static str> {
    for name in blobs.entry_names().map_err(|_| "disk_cache_unavailable")? {
        let name = name.to_string_lossy();
        if name.starts_with(".segment-") && name.ends_with(".tmp") {
            blobs.remove(&name).map_err(|_| "disk_cache_unavailable")?;
        }
    }
    blobs.sync().map_err(|_| "disk_cache_unavailable")
}

fn allocated_size(metadata: &fs::Metadata) -> Result<u64, &'static str> {
    metadata
        .blocks()
        .checked_mul(512)
        .ok_or("disk_cache_unavailable")
}

fn unix_milliseconds() -> Result<i64, &'static str> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "disk_cache_unavailable")
        .and_then(|duration| {
            i64::try_from(duration.as_millis()).map_err(|_| "disk_cache_unavailable")
        })
}

fn nonce() -> Result<u128, &'static str> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .map_err(|_| "disk_cache_unavailable")
}

fn hex_digest(digest: &[u8; 32]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = vec![0u8; 64];
    for (i, byte) in digest.iter().enumerate() {
        out[2 * i] = HEX[(byte >> 4) as usize];
        out[2 * i + 1] = HEX[(byte & 0x0f) as usize];
    }
    String::from_utf8(out).expect("hex encoding is valid UTF-8")
}

fn blob_name(digest: &[u8; 32]) -> String {
    format!("{}.bin", hex_digest(digest))
}

fn parse_blob_name(name: &str) -> Option<[u8; 32]> {
    let encoded = name.strip_suffix(".bin")?.as_bytes();
    if encoded.len() != 64 {
        return None;
    }
    let mut digest = [0; 32];
    for (output, pair) in digest.iter_mut().zip(encoded.chunks_exact(2)) {
        *output = hex_nibble(pair[0])? << 4 | hex_nibble(pair[1])?;
    }
    Some(digest)
}

fn hex_nibble(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::DiskSegmentCache;
    use crate::cache::{Admission, SegmentCacheKey, VerifiedSegment};
    use crate::yenc::DecodedPart;
    use std::io::{Seek, SeekFrom, Write};
    use std::os::unix::fs::PermissionsExt;
    use std::os::unix::fs::symlink;
    use std::path::PathBuf;

    fn root(label: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "comet-l2-{label}-{}-{}",
            std::process::id(),
            super::nonce().unwrap()
        ));
        std::fs::create_dir(&root).unwrap();
        root
    }

    fn key(value: u8) -> SegmentCacheKey {
        SegmentCacheKey([value; 32])
    }

    fn segment(value: u8, length: usize) -> VerifiedSegment {
        VerifiedSegment::from_decoded(DecodedPart {
            bytes: vec![value; length],
            begin: 1,
            end: length as u64,
            total_size: length as u64,
            expected_crc32: Some(u32::from(value)),
            expected_whole_crc32: Some(u32::from(value)),
        })
        .unwrap()
    }

    #[test]
    fn persists_verified_mappings_across_reopen() {
        let root = root("persist");
        {
            let mut cache = DiskSegmentCache::open(&root, 32 * 1024, 0).unwrap();
            assert_eq!(
                cache.insert(key(1), &segment(7, 1024)).unwrap(),
                Admission::Admitted
            );
            assert_eq!(cache.get(key(1)).unwrap().unwrap().to_decoded().bytes[0], 7);
        }
        let mut cache = DiskSegmentCache::open(&root, 32 * 1024, 0).unwrap();
        assert_eq!(cache.get(key(1)).unwrap().unwrap().to_decoded().bytes[0], 7);
        assert_eq!(cache.stats().unwrap().mappings, 1);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn physically_deduplicates_equal_verified_bytes_without_sharing_mappings() {
        let root = root("deduplicate");
        let mut cache = DiskSegmentCache::open(&root, 32 * 1024, 0).unwrap();
        cache.insert(key(1), &segment(7, 1024)).unwrap();
        cache.insert(key(2), &segment(7, 1024)).unwrap();

        let stats = cache.stats().unwrap();
        assert_eq!(stats.mappings, 2);
        assert_eq!(stats.blobs, 1);
        assert!(cache.get(key(1)).unwrap().is_some());
        assert!(cache.get(key(2)).unwrap().is_some());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn deduplicated_mapping_churn_remains_inside_the_disk_budget() {
        let root = root("mapping-churn");
        let budget = 32 * 1024;
        let mut cache = DiskSegmentCache::open(&root, budget, 0).unwrap();
        for value in 1..=64 {
            cache.insert(key(value), &segment(7, 1024)).unwrap();
        }

        let stats = cache.stats().unwrap();
        assert!(stats.used_bytes <= budget);
        assert!(stats.mappings <= budget as usize / super::DISK_MAPPING_OVERHEAD_BYTES as usize);
        assert_eq!(stats.blobs, 1);
        assert!(cache.get(key(1)).unwrap().is_none());
        assert!(cache.get(key(64)).unwrap().is_some());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn corrupt_blob_invalidates_every_mapping_and_is_removed() {
        let root = root("corrupt");
        let mut cache = DiskSegmentCache::open(&root, 32 * 1024, 0).unwrap();
        let value = segment(7, 1024);
        cache.insert(key(1), &value).unwrap();
        cache.insert(key(2), &value).unwrap();
        let mapping = cache
            .connection
            .query_row(
                "SELECT digest FROM acquisition_mappings LIMIT 1",
                [],
                |row| super::fixed_digest(row.get_ref(0)?.as_blob()?),
            )
            .unwrap();
        let path = cache.blob_path(&mapping);
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        let mut file = std::fs::OpenOptions::new().write(true).open(&path).unwrap();
        file.seek(SeekFrom::Start(0)).unwrap();
        file.write_all(b"corrupt").unwrap();
        file.sync_all().unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o400)).unwrap();

        assert_eq!(cache.get(key(1)).unwrap_err(), "disk_cache_corrupt");
        assert!(cache.get(key(2)).unwrap().is_none());
        assert_eq!(cache.stats().unwrap().mappings, 0);
        assert!(!path.exists());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn never_follows_a_blob_symlink_outside_the_confined_directory() {
        let root = root("blob-symlink");
        let mut cache = DiskSegmentCache::open(&root, 32 * 1024, 0).unwrap();
        cache.insert(key(1), &segment(7, 1024)).unwrap();
        let digest = cache
            .connection
            .query_row(
                "SELECT digest FROM acquisition_mappings LIMIT 1",
                [],
                |row| super::fixed_digest(row.get_ref(0)?.as_blob()?),
            )
            .unwrap();
        let blob = cache.blob_path(&digest);
        std::fs::remove_file(&blob).unwrap();
        let outside = root.join("outside");
        std::fs::write(&outside, b"outside").unwrap();
        symlink(&outside, &blob).unwrap();

        assert_eq!(cache.get(key(1)).unwrap_err(), "disk_cache_corrupt");
        assert_eq!(std::fs::read(&outside).unwrap(), b"outside");
        assert_eq!(cache.stats().unwrap().mappings, 0);
        assert!(!blob.exists());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn replaced_blob_directory_is_unavailable_without_invalidating_the_index() {
        let root = root("replaced-directory");
        let mut cache = DiskSegmentCache::open(&root, 32 * 1024, 0).unwrap();
        cache.insert(key(1), &segment(7, 1024)).unwrap();
        let digest = cache
            .connection
            .query_row(
                "SELECT digest FROM acquisition_mappings LIMIT 1",
                [],
                |row| super::fixed_digest(row.get_ref(0)?.as_blob()?),
            )
            .unwrap();
        let blob = cache.blob_path(&digest);
        let blobs = blob.parent().unwrap().to_path_buf();
        let displaced = blobs.with_extension("displaced");
        std::fs::rename(&blobs, &displaced).unwrap();
        std::fs::create_dir(&blobs).unwrap();

        assert!(matches!(cache.get(key(1)), Err("disk_cache_unavailable")));
        assert_eq!(cache.stats().unwrap().mappings, 1);
        assert!(displaced.join(blob.file_name().unwrap()).exists());
        assert_eq!(std::fs::read_dir(&blobs).unwrap().count(), 0);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn pressure_evicts_lru_mappings_to_ninety_percent() {
        let root = root("eviction");
        let mut cache = DiskSegmentCache::open(&root, 32 * 1024, 0).unwrap();
        for value in 1..=9 {
            cache.insert(key(value), &segment(value, 1024)).unwrap();
        }
        cache.insert(key(10), &segment(10, 1024)).unwrap();

        let stats = cache.stats().unwrap();
        assert!(stats.used_bytes <= 32 * 1024 * 9 / 10);
        assert!(cache.get(key(1)).unwrap().is_none());
        assert!(cache.get(key(10)).unwrap().is_some());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn pressure_evicts_more_than_one_bounded_lru_batch() {
        let root = root("batched-eviction");
        let budget = 64 * 1024;
        let mut cache = DiskSegmentCache::open(&root, budget, 0).unwrap();
        let value = segment(7, 1024);
        cache.insert(key(1), &value).unwrap();
        let digest = cache
            .connection
            .query_row("SELECT digest FROM blobs", [], |row| {
                super::fixed_digest(row.get_ref(0)?.as_blob()?)
            })
            .unwrap();
        cache
            .connection
            .execute(
                "
                WITH RECURSIVE sequence(value) AS (
                    VALUES(1)
                    UNION ALL
                    SELECT value + 1 FROM sequence WHERE value < 600
                )
                INSERT INTO acquisition_mappings(
                    acquisition_key, digest, begin_offset, end_offset, total_size,
                    part_crc32, whole_crc32, last_access_ms
                )
                SELECT CAST(printf('%032d', value) AS BLOB), ?1, 1, 1024, 1024, 7, 7, value
                FROM sequence
                ",
                rusqlite::params![digest.as_slice()],
            )
            .unwrap();

        cache.insert(key(255), &value).unwrap();

        assert!(cache.stats().unwrap().used_bytes <= budget * 9 / 10);
        assert!(cache.get(key(255)).unwrap().is_some());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn startup_removes_owned_temporary_and_orphan_files() {
        let root = root("temporary");
        let blobs = root.join("cache").join("segments").join("blobs");
        std::fs::create_dir_all(&blobs).unwrap();
        std::fs::write(blobs.join(".segment-abandoned.tmp"), b"x").unwrap();
        let orphan = blobs.join(format!("{}.bin", "a".repeat(64)));
        std::fs::write(&orphan, b"corrupt orphan").unwrap();
        std::fs::write(blobs.join("unrelated"), b"x").unwrap();

        let _cache = DiskSegmentCache::open(&root, 32 * 1024, 0).unwrap();

        assert!(!blobs.join(".segment-abandoned.tmp").exists());
        assert!(!orphan.exists());
        assert!(blobs.join("unrelated").exists());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn startup_invalidates_an_indexed_blob_missing_after_a_crash() {
        let root = root("missing-indexed");
        let path = {
            let mut cache = DiskSegmentCache::open(&root, 32 * 1024, 0).unwrap();
            cache.insert(key(1), &segment(7, 1024)).unwrap();
            let digest = cache
                .connection
                .query_row(
                    "SELECT digest FROM acquisition_mappings LIMIT 1",
                    [],
                    |row| super::fixed_digest(row.get_ref(0)?.as_blob()?),
                )
                .unwrap();
            cache.blob_path(&digest)
        };
        std::fs::remove_file(path).unwrap();

        let cache = DiskSegmentCache::open(&root, 32 * 1024, 0).unwrap();

        assert_eq!(cache.stats().unwrap().mappings, 0);
        assert_eq!(cache.stats().unwrap().blobs, 0);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn startup_finishes_committed_eviction_file_cleanup() {
        let root = root("committed-eviction");
        let blob = {
            let mut cache = DiskSegmentCache::open(&root, 32 * 1024, 0).unwrap();
            cache.insert(key(1), &segment(7, 1024)).unwrap();
            let digest = cache
                .connection
                .query_row(
                    "SELECT digest FROM acquisition_mappings LIMIT 1",
                    [],
                    |row| super::fixed_digest(row.get_ref(0)?.as_blob()?),
                )
                .unwrap();
            cache
                .connection
                .execute("DELETE FROM acquisition_mappings", [])
                .unwrap();
            let blob = cache.blob_path(&digest);
            assert!(blob.exists());
            blob
        };

        let cache = DiskSegmentCache::open(&root, 32 * 1024, 0).unwrap();

        assert_eq!(cache.stats().unwrap().mappings, 0);
        assert_eq!(cache.stats().unwrap().blobs, 0);
        assert!(!blob.exists());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn free_space_reserve_bypasses_new_blobs_and_deduplicated_mappings() {
        let root = root("free-space");
        let mut cache = DiskSegmentCache::open(&root, 32 * 1024, 0).unwrap();
        assert_eq!(
            cache.insert(key(1), &segment(7, 1024)).unwrap(),
            Admission::Admitted
        );
        cache.minimum_free_bytes = u64::MAX;

        assert_eq!(
            cache.insert(key(2), &segment(7, 1024)).unwrap(),
            Admission::Bypassed
        );
        assert_eq!(
            cache.insert(key(3), &segment(8, 1024)).unwrap(),
            Admission::Bypassed
        );
        assert_eq!(cache.stats().unwrap().mappings, 1);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn refuses_a_symlinked_sqlite_index() {
        let root = root("index-symlink");
        let cache_root = root.join("cache").join("segments");
        std::fs::create_dir_all(&cache_root).unwrap();
        let outside = root.join("outside");
        std::fs::write(&outside, b"unchanged").unwrap();
        symlink(&outside, cache_root.join("index.sqlite3")).unwrap();

        assert!(DiskSegmentCache::open(&root, 32 * 1024, 0).is_err());
        assert_eq!(std::fs::read(outside).unwrap(), b"unchanged");
        let _ = std::fs::remove_dir_all(root);
    }
}
